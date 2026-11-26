"""The demo film's sidecar (`data/media/eddie_demo.json`) and the YouTube ID parser.

Two modes, chosen by the sidecar and served by `/api/demo-video/meta`:

* local: the MP4 next to the sidecar is streamed by the API (with byte ranges). This is the
  mode while the site is behind Basic Auth and the cut is still a draft.
* youtube: the film is embedded from youtube-nocookie.com; the local MP4 is neither served nor
  shipped to the cluster. The local poster stays, so the card itself makes no third-party
  request until someone presses play.

Switching modes rewrites the sidecar only (`scripts/sync_demo_video.sh --youtube <url>`), which
the API reads per request: a data sync is enough, no pod restarts. `EDDIE_DEMO_YOUTUBE` (an ID
or URL) overrides the sidecar, and `EDDIE_DEMO_VIDEO_URL` (an https .mp4) points the local
player at external storage instead of the pod.

Only the standard library is used here, so the read-only API can import it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

VIDEO_NAME = "eddie_demo.mp4"
POSTER_NAME = "eddie_demo_poster.jpg"
SIDECAR_NAME = "eddie_demo.json"
TITLE = "A Fistful of Tokens"
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
                 "www.youtube-nocookie.com"}
MODES = ("local", "youtube")


class MediaError(ValueError):
    pass


def parse_youtube_id(text: str) -> str:
    """An 11-character video ID from a bare ID or a youtu.be, watch?v=, embed/, shorts/ or live/ URL."""
    raw = (text or "").strip()
    if YOUTUBE_ID.fullmatch(raw):
        return raw
    url = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (url.hostname or "").lower()
    candidate = None
    if host == "youtu.be":
        candidate = url.path.lstrip("/").split("/")[0]
    elif host in YOUTUBE_HOSTS:
        if url.path == "/watch":
            candidate = (parse_qs(url.query).get("v") or [""])[0]
        else:
            parts = [p for p in url.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("embed", "shorts", "live", "v"):
                candidate = parts[1]
    if candidate and YOUTUBE_ID.fullmatch(candidate):
        return candidate
    raise MediaError(f"not a YouTube video URL or ID: {raw[:80]!r}")


def embed_src(video_id: str) -> str:
    return f"https://www.youtube-nocookie.com/embed/{video_id}?autoplay=1&rel=0"


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def duration_label(seconds: float | None) -> str | None:
    if not seconds or seconds <= 0:
        return None
    total = round(seconds)
    return f"{total // 60}:{total % 60:02d}"


def media_dir() -> Path:
    from eddie import paths

    return Path(os.environ.get("EDDIE_MEDIA_DIR") or paths.data_dir() / "media")


def read_sidecar(directory: Path) -> dict[str, Any]:
    try:
        data = json.loads((directory / SIDECAR_NAME).read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def external_video_url() -> str | None:
    """EDDIE_DEMO_VIDEO_URL, only when it is an https URL of an .mp4 file."""
    raw = (os.environ.get("EDDIE_DEMO_VIDEO_URL") or "").strip()
    if not raw:
        return None
    url = urlparse(raw)
    if url.scheme == "https" and url.hostname and url.path.lower().endswith(".mp4"):
        return raw
    print(f"warning: ignoring EDDIE_DEMO_VIDEO_URL={raw[:80]!r}: it must be an https URL of an .mp4 file")
    return None


def mode_and_id(sidecar: dict[str, Any]) -> tuple[str, str | None]:
    override = (os.environ.get("EDDIE_DEMO_YOUTUBE") or "").strip()
    if override:
        try:
            return "youtube", parse_youtube_id(override)
        except MediaError:
            print(f"warning: ignoring EDDIE_DEMO_YOUTUBE={override[:80]!r}: not a YouTube URL or ID")
    if sidecar.get("mode") == "youtube":
        try:
            return "youtube", parse_youtube_id(str(sidecar.get("youtube_id") or ""))
        except MediaError:
            return "local", None
    return "local", None


def meta(directory: Path | None = None) -> dict[str, Any]:
    """What the film card needs. `{"available": false}` whenever there is nothing to play."""
    directory = directory or media_dir()
    sidecar = read_sidecar(directory)
    video, poster = directory / VIDEO_NAME, directory / POSTER_NAME
    has_poster = poster.is_file()
    version = str(sidecar.get("sha256") or "")[:12]
    if not version and video.is_file():
        st = video.stat()
        version = f"{int(st.st_mtime)}-{st.st_size}"
    poster_url = os.environ.get("EDDIE_DEMO_POSTER_URL") or (f"/api/demo-video/poster?v={version or 'x'}" if has_poster else None)
    base = {
        "title": str(sidecar.get("title") or TITLE),
        "duration_s": sidecar.get("duration_s"),
        "duration_label": duration_label(sidecar.get("duration_s")),
        "width": sidecar.get("width"),
        "height": sidecar.get("height"),
        "cut": sidecar.get("cut") or "draft",
        "poster": poster_url is not None,
        "poster_src": poster_url,
        "version": version or None,
    }
    mode, video_id = mode_and_id(sidecar)
    if mode == "youtube" and video_id:
        return {"available": True, "mode": "youtube", "youtube_id": video_id, "embed_src": embed_src(video_id),
                "watch_url": watch_url(video_id), "external": True, **base, "cut": sidecar.get("cut") or "final"}
    external = external_video_url()
    if external:
        return {"available": True, "mode": "local", "src": external, "external": True, **base}
    if not video.is_file():
        return {"available": False}
    return {"available": True, "mode": "local", "src": f"/api/demo-video?v={version}", "external": False,
            "bytes": video.stat().st_size, **base}


def serves_local_file(directory: Path | None = None) -> bool:
    """The MP4 itself is served only in local mode (never once the film lives on YouTube)."""
    directory = directory or media_dir()
    mode, video_id = mode_and_id(read_sidecar(directory))
    return not (mode == "youtube" and video_id) and (directory / VIDEO_NAME).is_file()
