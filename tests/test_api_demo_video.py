"""The demo film: YouTube ID parsing, the two modes, byte ranges, and the public-serving headers."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from eddie import media
from eddie.media import MediaError, parse_youtube_id

VIDEO_ID = "dQw4w9WgXcQ"  # an 11-character ID shape; never fetched here


@pytest.mark.parametrize(
    "text",
    [
        VIDEO_ID,
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}&t=42s&list=PL123",
        f"https://m.youtube.com/watch?feature=share&v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?si=abcdef",
        f"youtu.be/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}?autoplay=1",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"  https://www.youtube.com/live/{VIDEO_ID}  ",
    ],
)
def test_every_youtube_url_shape_gives_the_id(text):
    assert parse_youtube_id(text) == VIDEO_ID


@pytest.mark.parametrize(
    "text",
    [
        "", "dQw4w9WgXc", "dQw4w9WgXcQQ", "dQw4w9WgX!Q", "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=short", "https://youtu.be/", "https://www.youtube.com/channel/UCabcdefghij",
        "javascript:alert(1)", "https://evil.example/youtu.be/dQw4w9WgXcQ",
    ],
)
def test_anything_else_is_refused(text):
    with pytest.raises(MediaError):
        parse_youtube_id(text)


@pytest.fixture
def app(tmp_path, monkeypatch):
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><div id=root></div>")
    (web / "assets" / "app-abc123.js").write_text("console.log('eddie')")
    (web / "audio" / "tour").mkdir(parents=True)
    (web / "audio" / "tour" / "shot_1.mp3").write_bytes(b"ID3fake")
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    monkeypatch.setenv("EDDIE_MEDIA_DIR", str(media_dir))
    monkeypatch.setenv("EDDIE_WEB_DIST", str(web))
    monkeypatch.setenv("EDDIE_INDEX_PATH", str(tmp_path / "absent.sqlite"))
    monkeypatch.setenv("EDDIE_PUBLIC_DIR", str(tmp_path / "public"))
    for key in ("EDDIE_DEMO_YOUTUBE", "EDDIE_DEMO_VIDEO_URL", "EDDIE_DEMO_POSTER_URL", "EDDIE_PUBLIC_CACHE", "EDDIE_DOCS"):
        monkeypatch.delenv(key, raising=False)

    def client():
        from eddie.api.app import create_app

        return TestClient(create_app())

    return client, media_dir


FILM = bytes(range(256)) * 40  # 10,240 bytes standing in for an MP4
POSTER = b"\xff\xd8\xff\xe0" + b"\x00" * 300


def write_film(media_dir, **sidecar):
    (media_dir / media.VIDEO_NAME).write_bytes(FILM)
    (media_dir / media.POSTER_NAME).write_bytes(POSTER)
    base = {"title": "A Fistful of Tokens", "duration_s": 118.0, "width": 1280, "height": 720, "sha256": "a1b2c3d4e5f6" + "0" * 52,
            "cut": "draft", "mode": "local"}
    base.update(sidecar)
    (media_dir / media.SIDECAR_NAME).write_text(json.dumps(base))


def test_no_film_means_available_false_and_404s(app):
    client, _ = app
    c = client()
    assert c.get("/api/demo-video/meta").json() == {"available": False}
    assert c.get("/api/demo-video").status_code == 404
    assert c.get("/api/demo-video/poster").status_code == 404


def test_local_mode_serves_the_film_with_byte_ranges(app):
    client, media_dir = app
    write_film(media_dir)
    c = client()
    m = c.get("/api/demo-video/meta").json()
    assert m["available"] and m["mode"] == "local" and m["external"] is False
    assert m["src"] == "/api/demo-video?v=a1b2c3d4e5f6" and m["poster_src"] == "/api/demo-video/poster?v=a1b2c3d4e5f6"
    assert m["duration_label"] == "1:58" and m["cut"] == "draft" and m["bytes"] == len(FILM) and m["title"] == "A Fistful of Tokens"
    part = c.get(m["src"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.headers["content-range"] == f"bytes 0-99/{len(FILM)}"
    assert part.content == FILM[:100] and part.headers["content-type"] == "video/mp4"
    whole = c.get(m["src"])
    assert whole.status_code == 200 and whole.headers["accept-ranges"] == "bytes" and whole.content == FILM
    assert "public" not in whole.headers["cache-control"]  # behind Basic Auth no shared cache may keep it
    poster = c.get(m["poster_src"])
    assert poster.status_code == 200 and poster.headers["content-type"] == "image/jpeg"


def test_youtube_mode_embeds_and_stops_serving_the_mp4(app):
    client, media_dir = app
    write_film(media_dir, mode="youtube", youtube_id=VIDEO_ID, cut="final")
    c = client()
    m = c.get("/api/demo-video/meta").json()
    assert m["available"] and m["mode"] == "youtube" and m["youtube_id"] == VIDEO_ID and m["cut"] == "final"
    assert m["embed_src"] == f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}?autoplay=1&rel=0"
    assert m["watch_url"] == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert m["poster_src"].startswith("/api/demo-video/poster")  # the card still loads with no third-party request
    assert "src" not in m
    assert c.get("/api/demo-video").status_code == 404  # nobody pulls the file from the home lab any more
    assert c.get("/api/demo-video/poster").status_code == 200


def test_youtube_mode_needs_no_local_file_and_the_env_overrides_the_sidecar(app, monkeypatch):
    client, media_dir = app
    monkeypatch.setenv("EDDIE_DEMO_YOUTUBE", f"https://youtu.be/{VIDEO_ID}")
    m = client().get("/api/demo-video/meta").json()
    assert m["available"] and m["mode"] == "youtube" and m["poster"] is False and m["poster_src"] is None
    monkeypatch.setenv("EDDIE_DEMO_YOUTUBE", "not a video")
    assert client().get("/api/demo-video/meta").json() == {"available": False}


def test_a_broken_youtube_sidecar_falls_back_to_the_local_film(app):
    client, media_dir = app
    write_film(media_dir, mode="youtube", youtube_id="nope")
    m = client().get("/api/demo-video/meta").json()
    assert m["mode"] == "local" and m["src"].startswith("/api/demo-video")


def test_an_external_mp4_url_is_used_only_when_it_is_https(app, monkeypatch):
    client, media_dir = app
    monkeypatch.setenv("EDDIE_DEMO_VIDEO_URL", "https://media.example.com/eddie_demo.mp4")
    m = client().get("/api/demo-video/meta").json()
    assert m["available"] and m["mode"] == "local" and m["external"] and m["src"] == "https://media.example.com/eddie_demo.mp4"
    for bad in ("http://media.example.com/eddie_demo.mp4", "https://media.example.com/page.html"):
        monkeypatch.setenv("EDDIE_DEMO_VIDEO_URL", bad)
        assert client().get("/api/demo-video/meta").json() == {"available": False}


def test_headers_let_youtube_embed_and_stay_private_until_the_unlock(app, monkeypatch):
    client, media_dir = app
    write_film(media_dir)
    c = client()
    root = c.get("/")
    assert root.headers["referrer-policy"] == "strict-origin-when-cross-origin"  # never no-referrer (YouTube Error 153)
    assert "content-security-policy" not in root.headers  # nothing blocks the youtube-nocookie frame
    assert root.headers["cache-control"] == "no-store"
    assert "public" not in c.get("/assets/app-abc123.js").headers.get("cache-control", "")
    monkeypatch.setenv("EDDIE_PUBLIC_CACHE", "1")
    c = client()
    assert c.get("/assets/app-abc123.js").headers["cache-control"] == "public, max-age=31536000, immutable"
    assert c.get("/audio/tour/shot_1.mp3").headers["cache-control"] == "public, max-age=3600"
    assert c.get("/api/demo-video/meta").headers["cache-control"] == "public, max-age=60"
    assert c.get("/api/demo-video").headers["cache-control"] == "public, max-age=3600"
    assert c.get("/").headers["cache-control"] == "no-store"  # the page itself is never cached


def test_api_docs_can_be_switched_off(app, monkeypatch):
    client, _ = app
    assert client().get("/api/openapi.json").status_code == 200
    monkeypatch.setenv("EDDIE_DOCS", "0")
    c = client()
    assert c.get("/api/openapi.json").status_code == 404 and c.get("/api/docs").status_code == 404
