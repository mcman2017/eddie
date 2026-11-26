"""`scripts/sync_demo_video.sh`: put the demo film where the explorer serves it from.

Local mode copies the latest cut out of the film project (`~/eddie-demo/out`, which is only
ever read) into `data/media/`: the final `eddie_demo.mp4` when it exists, otherwise the 720p
draft. The file is remuxed with `+faststart` (playback starts before the whole file arrives)
and its metadata stripped, without re-encoding; a final cut that is too large for a home uplink
is transcoded to 720p instead. A poster is taken from the title card at 0:04.

YouTube mode only rewrites the sidecar: the film card embeds the public upload, the local
poster stays (no third-party request until someone presses play), and the MP4 stops being
served or shipped. `scripts/deploy_prod.sh sync` then carries either to the cluster.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eddie import media

MAX_REMUX_BYTES = 80 * 1024 * 1024
MAX_HEIGHT = 1080
POSTER_AT_S = 4.0  # the title card "A FISTFUL OF TOKENS" is on screen from 2.5 s to 7.5 s


class SyncError(RuntimeError):
    pass


def _run(argv: list[str]) -> str:
    res = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=900)
    if res.returncode != 0:
        raise SyncError(f"{Path(argv[0]).name} failed: {res.stderr.strip()[-400:]}")
    return res.stdout


def probe(path: Path) -> dict[str, Any]:
    raw = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]))
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in raw.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise SyncError(f"{path.name} has no video stream")
    return {
        "duration_s": round(float(raw.get("format", {}).get("duration") or 0.0), 3),
        "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
        "vcodec": video.get("codec_name"), "pix_fmt": video.get("pix_fmt"),
        "acodec": audio.get("codec_name") if audio else None, "bytes": path.stat().st_size,
    }


def choose_source(demo_dir: Path) -> tuple[Path, str]:
    final, draft = demo_dir / "out" / "eddie_demo.mp4", demo_dir / "out" / "eddie_demo_720p.mp4"
    if final.is_file():
        return final, "final"
    if draft.is_file():
        return draft, "draft"
    raise SyncError(f"no film in {demo_dir / 'out'} (looked for eddie_demo.mp4, then eddie_demo_720p.mp4)")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def write_sidecar(media_dir: Path, data: dict[str, Any]) -> None:
    media_dir.mkdir(parents=True, exist_ok=True)
    tmp = media_dir / f".{media.SIDECAR_NAME}.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, media_dir / media.SIDECAR_NAME)


def extract_poster(video: Path, poster: Path, at_s: float) -> None:
    tmp = poster.with_name(f".{poster.name}.tmp.jpg")
    _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at_s:.2f}", "-i", str(video), "-frames:v", "1",
          "-vf", "scale=1280:-2", "-q:v", "3", str(tmp)])
    os.replace(tmp, poster)


def now() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")


def sync_local(*, media_dir: Path, demo_dir: Path, src: Path | None = None, poster_at: float = POSTER_AT_S,
               log: Callable[[str], None] = print) -> dict[str, Any]:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SyncError(f"{tool} is missing")
    if src is not None:
        cut = "final" if src.name == "eddie_demo.mp4" else "draft"
    else:
        src, cut = choose_source(demo_dir)
    info = probe(src)
    if not 30 <= info["duration_s"] <= 300:
        raise SyncError(f"{src.name} lasts {info['duration_s']} s; a demo film is 30 s to 5 min")
    media_dir.mkdir(parents=True, exist_ok=True)
    dst = media_dir / media.VIDEO_NAME
    tmp = media_dir / f".{media.VIDEO_NAME}.tmp.mp4"
    web_ready = info["vcodec"] == "h264" and info["pix_fmt"] == "yuv420p" and info["acodec"] in ("aac", None)
    if web_ready and info["bytes"] <= MAX_REMUX_BYTES and info["height"] <= MAX_HEIGHT:
        log(f"remuxing {src.name} ({info['bytes'] / 1e6:.1f} MB, {info['width']}x{info['height']}) with +faststart")
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
              "-movflags", "+faststart", "-map_metadata", "-1", str(tmp)])
    else:
        log(f"transcoding {src.name} ({info['bytes'] / 1e6:.1f} MB, {info['width']}x{info['height']}, {info['vcodec']}) "
            "to 720p H.264 for the web")
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
              "-preset", "medium", "-crf", "21", "-vf", "scale=-2:720", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a",
              "128k", "-movflags", "+faststart", "-map_metadata", "-1", str(tmp)])
    os.replace(tmp, dst)
    out = probe(dst)
    extract_poster(dst, media_dir / media.POSTER_NAME, poster_at)
    sidecar = {
        "title": media.TITLE, "mode": "local", "cut": cut, "source": src.name, "duration_s": out["duration_s"],
        "width": out["width"], "height": out["height"], "bytes": out["bytes"], "sha256": _sha256(dst),
        "poster_at_s": poster_at, "synced_at": now(),
    }
    write_sidecar(media_dir, sidecar)
    log(f"film: {media.duration_label(out['duration_s'])}, {out['width']}x{out['height']}, {out['bytes'] / 1e6:.1f} MB "
        f"({cut} cut) -> {dst}")
    return sidecar


def sync_youtube(url: str, *, media_dir: Path, log: Callable[[str], None] = print,
                 poster_at: float = POSTER_AT_S) -> dict[str, Any]:
    video_id = media.parse_youtube_id(url)
    sidecar = media.read_sidecar(media_dir)
    poster = media_dir / media.POSTER_NAME
    if not poster.is_file() and (media_dir / media.VIDEO_NAME).is_file():
        extract_poster(media_dir / media.VIDEO_NAME, poster, poster_at)
    if not poster.is_file():
        log("warning: no local poster: the card will show its title only (run a local sync first to get one)")
    sidecar.update({"title": sidecar.get("title") or media.TITLE, "mode": "youtube", "youtube_id": video_id,
                    "cut": "final", "synced_at": now()})
    write_sidecar(media_dir, sidecar)
    log(f"film: YouTube {video_id} ({media.watch_url(video_id)}); the local MP4 is no longer served or shipped")
    return sidecar
