"""Data releases for the cluster, and the film sync: only an allowlist ships, every database opens
read-only without help, and YouTube mode keeps the MP4 at home."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess

import pytest

from eddie import media
from eddie.config import load_settings
from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone
from eddie.ingest.synthetic import write_sample_tree
from eddie.media_sync import probe, sync_local, sync_youtube
from eddie.release import ReleaseError, build_release
from eddie.state.artifacts import ArtifactStore
from tests.helpers import make_png

pytestmark = pytest.mark.slow
FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("release")
    write_sample_tree(root / "tree", scale=2)
    index = root / "index.sqlite"
    build_index(root / "tree", index, workers=1, log=lambda *_: None)
    con = sqlite3.connect(index)
    names = [r[0] for r in con.execute("SELECT full_name FROM decls WHERE full_name IS NOT NULL")]
    con.close()
    spine = root / "spine.tsv.gz"
    with gzip.open(spine, "wt") as fh:
        fh.writelines(f"{n}\tthm\n" for n in names if not n.startswith("Edge."))
    cone = root / "cone.sqlite"
    build_cone(index, cone, spine_path=spine, workers=1, log=lambda *_: None)
    return index, cone


@pytest.fixture
def data(built, tmp_path, monkeypatch):
    index, cone = built
    d = tmp_path / "data"  # conftest's EDDIE_DATA_DIR
    (d / "index" / "landscape").mkdir(parents=True)
    shutil.copy(index, d / "index" / "eddie_index.sqlite")
    shutil.copy(cone, d / "index" / "eddie_cone.sqlite")
    (d / "index" / "work_plan.json").write_text("{}")
    (d / "index" / "landscape" / "landscape.json").write_text("{}")
    png = make_png(32, 24)
    sha = hashlib.sha256(png).hexdigest()
    (d / "artifacts").mkdir()
    (d / "artifacts" / f"{sha}.png").write_bytes(png)
    (d / "artifacts" / ("ff" * 32 + ".png")).write_bytes(png + b"unreferenced")
    store = ArtifactStore(d / "state" / "eddie_artifacts.sqlite")
    store.add_witness("riemannHypothesis", kind="plot", artifact_sha256=sha, ext="png", source="seed", status="verified")
    (d / "state" / "rehearsal.sqlite").write_bytes(b"not shipped")
    (d / "shadow.sqlite").write_bytes(b"not shipped")
    settings = load_settings()
    settings.public_dir.mkdir(parents=True, exist_ok=True)
    (settings.public_dir / "status.json").write_text('{"committed_usd": 0.01}')
    media_dir = d / "media"
    monkeypatch.setenv("EDDIE_MEDIA_DIR", str(media_dir))
    media_dir.mkdir()
    (media_dir / media.VIDEO_NAME).write_bytes(b"\x00" * 64)
    (media_dir / media.POSTER_NAME).write_bytes(b"\xff\xd8poster")
    (media_dir / media.SIDECAR_NAME).write_text(json.dumps({"mode": "local", "cut": "draft"}))
    return d, settings, media_dir, sha


def test_a_release_ships_the_allowlist_and_nothing_else(data, tmp_path):
    d, settings, _, sha = data
    out = tmp_path / "rel"
    info = build_release(settings, out)
    shipped = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert shipped == sorted([
        "MANIFEST.sha256", "RELEASE.json", "artifacts/" + sha + ".png", "index/eddie_cone.sqlite",
        "index/eddie_index.sqlite", "index/landscape/landscape.json", "index/work_plan.json",
        "media/eddie_demo.json", "media/eddie_demo.mp4", "media/eddie_demo_poster.jpg", "public/status.json",
        "state/eddie_artifacts.sqlite",
    ])
    assert info["artifacts"] == 1 and info["media_mode"] == "local"
    for db in out.rglob("*.sqlite"):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        con.close()
        assert not any(db.with_name(db.name + s).exists() for s in ("-wal", "-shm", "-journal"))
    for line in (out / "MANIFEST.sha256").read_text().splitlines():
        digest, rel = line.split("  ", 1)
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == digest, rel
    ok = subprocess.run(["sha256sum", "-c", "--quiet", "MANIFEST.sha256"], cwd=out, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr  # exactly what the loader pod runs


def test_youtube_mode_keeps_the_film_at_home(data, tmp_path):
    _, settings, media_dir, _ = data
    (media_dir / media.SIDECAR_NAME).write_text(json.dumps({"mode": "youtube", "youtube_id": "dQw4w9WgXcQ"}))
    info = build_release(settings, tmp_path / "rel")
    assert info["media_mode"] == "youtube"
    assert not (tmp_path / "rel" / "media" / media.VIDEO_NAME).exists()
    assert (tmp_path / "rel" / "media" / media.POSTER_NAME).exists()


def test_a_busy_database_is_refused_and_nothing_is_left_behind(data, tmp_path):
    d, settings, _, _ = data
    (d / "index" / "eddie_cone.sqlite-journal").write_bytes(b"hot")
    with pytest.raises(ReleaseError, match="journal"):
        build_release(settings, tmp_path / "rel")
    assert not (tmp_path / "rel").exists()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_the_film_sync_remuxes_takes_a_poster_and_switches_to_youtube(tmp_path):
    demo = tmp_path / "eddie-demo"
    (demo / "out").mkdir(parents=True)
    src = demo / "out" / "eddie_demo_720p.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=5:duration=31",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=31", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    out = tmp_path / "media"
    side = sync_local(media_dir=out, demo_dir=demo, log=lambda *_: None)
    assert side["mode"] == "local" and side["cut"] == "draft" and side["source"] == src.name
    assert (out / media.POSTER_NAME).stat().st_size > 0 and probe(out / media.VIDEO_NAME)["vcodec"] == "h264"
    with open(out / media.VIDEO_NAME, "rb") as fh:  # +faststart: the index comes before the media data
        head = fh.read(4096)
    assert head.find(b"moov") != -1 and (head.find(b"mdat") == -1 or head.find(b"moov") < head.find(b"mdat"))
    assert src.stat().st_size > 0 and sorted(p.name for p in (demo / "out").iterdir()) == [src.name]  # read only
    yt = sync_youtube("https://youtu.be/dQw4w9WgXcQ", media_dir=out, log=lambda *_: None)
    assert yt["mode"] == "youtube" and yt["youtube_id"] == "dQw4w9WgXcQ" and yt["cut"] == "final"
    meta = media.meta(out)
    assert meta["mode"] == "youtube" and meta["poster"] and not media.serves_local_file(out)
