"""A read-only data release for the cluster: `python -m eddie.cli release build --out DIR`.

`scripts/deploy_prod.sh sync` ships what this builds into the pod's volume. Only an explicit
allowlist goes in, never the ledger, the anchor, the key, the shadow ledger or the rehearsal
database:

    index/eddie_index.sqlite  index/eddie_cone.sqlite  index/work_plan.json  index/landscape/*
    state/eddie_artifacts.sqlite      (SQLite online backup: consistent while a run writes)
    artifacts/<sha256>.<ext>          (only files the artifacts database references)
    public/status.json  public/telemetry.json   (the governor's public snapshots)
    media/eddie_demo.json  media/eddie_demo_poster.jpg  media/eddie_demo.mp4 (not in youtube mode)
    MANIFEST.sha256  RELEASE.json

Every SQLite file in a release is in rollback-journal mode with no -wal, -shm or -journal file
beside it, so the pod can open it `mode=ro` on a read-only mount without creating anything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from eddie import media, paths
from eddie.config import Settings

SIDECARS = ("-wal", "-shm", "-journal")
STATIC_DBS = ("eddie_index.sqlite", "eddie_cone.sqlite")


class ReleaseError(RuntimeError):
    pass


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _check_sqlite(path: Path) -> None:
    for suffix in SIDECARS:
        if Path(f"{path}{suffix}").exists():
            raise ReleaseError(f"{path.name} has a {suffix} file beside it: a writer is busy or crashed; retry later")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    finally:
        con.close()
    if mode != "delete":
        raise ReleaseError(f"{path.name} is in journal_mode={mode}; a read-only mount needs delete")


def snapshot(src: Path, dst: Path) -> None:
    """Online backup (consistent against concurrent writers), then rollback-journal mode."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
        d.commit()
    finally:
        d.close()
        s.close()
    _check_sqlite(dst)


class HashCache:
    """sha256 of large, rarely changing files, keyed by (path, size, mtime)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data: dict[str, list[Any]] = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}

    def sha256(self, file: Path) -> str:
        st = file.stat()
        key = str(file.resolve())
        hit = self.data.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2]
        h = hashlib.sha256()
        with open(file, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
        self.data[key] = [st.st_size, st.st_mtime_ns, h.hexdigest()]
        return h.hexdigest()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(paths.repo_root()), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def build_release(settings: Settings, out: Path, *, media_dir: Path | None = None) -> dict[str, Any]:
    data = settings.data_dir
    index_dir = data / "index"
    media_dir = media_dir or media.media_dir()
    if out.exists():
        raise ReleaseError(f"{out} already exists")
    for name in STATIC_DBS:
        if not (index_dir / name).exists():
            raise ReleaseError(f"missing {index_dir / name}: build the index and the cone first")
    out.mkdir(parents=True)
    try:
        # the index and the cone are rebuilt only by hand: link them, after checking nobody is writing
        for name in STATIC_DBS:
            _check_sqlite(index_dir / name)
            _link_or_copy(index_dir / name, out / "index" / name)
        for extra in ("work_plan.json",):
            if (index_dir / extra).exists():
                shutil.copy2(index_dir / extra, out / "index" / extra)
        landscape = index_dir / "landscape"
        if landscape.is_dir():
            for f in sorted(landscape.iterdir()):
                if f.is_file():
                    _link_or_copy(f, out / "index" / "landscape" / f.name)
        artifacts_db = data / "state" / "eddie_artifacts.sqlite"
        referenced: list[str] = []
        if artifacts_db.exists():
            snapshot(artifacts_db, out / "state" / "eddie_artifacts.sqlite")
            con = sqlite3.connect(f"file:{out / 'state' / 'eddie_artifacts.sqlite'}?mode=ro", uri=True)
            try:
                referenced = sorted({f"{r[0]}.{r[1]}" for r in con.execute("SELECT artifact_sha256, ext FROM witnesses")})
            finally:
                con.close()
        missing = []
        for name in referenced:
            src = data / "artifacts" / name
            if src.is_file():
                _link_or_copy(src, out / "artifacts" / name)
            else:
                missing.append(name)
        (out / "public").mkdir()
        for name in ("status.json", "telemetry.json"):
            src = settings.public_dir / name
            if src.is_file():
                shutil.copy2(src, out / "public" / name)
        sidecar = media.read_sidecar(media_dir)
        youtube = media.mode_and_id(sidecar)[0] == "youtube"
        for name in (media.SIDECAR_NAME, media.POSTER_NAME, media.VIDEO_NAME):
            src = media_dir / name
            if not src.is_file() or (youtube and name == media.VIDEO_NAME):
                continue  # in youtube mode the film never leaves this machine
            _link_or_copy(src, out / "media" / name)
        cache = HashCache(data / "deploy" / "hash-cache.json")
        files = sorted(p for p in out.rglob("*") if p.is_file())
        manifest = [(cache.sha256(p), str(p.relative_to(out))) for p in files]
        cache.save()
        (out / "MANIFEST.sha256").write_text("".join(f"{h}  {rel}\n" for h, rel in manifest))
        info = {
            "id": out.name, "created_at": dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds"), "git": _git_sha(),
            "files": len(manifest), "bytes": sum(p.stat().st_size for p in files), "artifacts": len(referenced) - len(missing),
            "missing_artifacts": missing, "media_mode": "youtube" if youtube else ("local" if sidecar else None),
        }
        (out / "RELEASE.json").write_text(json.dumps(info, indent=2))
        forbidden = [p for p in out.rglob("*") if p.name in ("ledger.sqlite", "shadow.sqlite", "rehearsal.sqlite")
                     or p.suffix in (".key", ".pem")]
        if forbidden:
            raise ReleaseError(f"refusing to ship {forbidden[0]}")
        return info
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
