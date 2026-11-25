"""Content-addressed artifact store for sandbox outputs."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

ALLOWED_EXTENSIONS = {".png": "image", ".gif": "image", ".wav": "audio", ".json": "data", ".csv": "data", ".txt": "text"}
INTERNAL_FILES = {"main.py", ".sandbox_violations"}
INTERNAL_DIRS = {"tmp", ".mpl", "__pycache__"}


@dataclass(frozen=True)
class Artifact:
    name: str
    kind: str
    sha256: str
    bytes: int
    store_path: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CollectLimits:
    max_files: int = 64
    max_file_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 200 * 1024 * 1024


def collect(job_dir: Path, store: Path, limits: CollectLimits = CollectLimits()) -> tuple[list[Artifact], list[str]]:
    """Copy allowed regular files (no symlinks) into `store/<sha256><ext>`."""
    store.mkdir(parents=True, exist_ok=True)
    artifacts: list[Artifact] = []
    rejected: list[str] = []
    total = 0
    for root, dirs, files in os.walk(job_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in INTERNAL_DIRS)
        for name in sorted(files):
            path = Path(root) / name
            rel = str(path.relative_to(job_dir))
            if rel in INTERNAL_FILES:
                continue
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                rejected.append(f"{rel}: not a regular file")
                continue
            ext = path.suffix.lower()
            kind = ALLOWED_EXTENSIONS.get(ext)
            if kind is None:
                rejected.append(f"{rel}: extension {ext or '(none)'} not allowed")
                continue
            if st.st_size > limits.max_file_bytes:
                rejected.append(f"{rel}: {st.st_size} bytes exceeds the per-file cap")
                continue
            if len(artifacts) >= limits.max_files or total + st.st_size > limits.max_total_bytes:
                rejected.append(f"{rel}: artifact count/size budget exhausted")
                continue
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            dest = store / f"{digest}{ext}"
            if not dest.exists():
                tmp = dest.with_name(dest.name + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, dest)
            total += len(data)
            artifacts.append(Artifact(rel, kind, digest, len(data), str(dest)))
    return artifacts, rejected


def sweep_stale(tmp_root: Path, *, prefix: str = "eddie-sbx-", max_age_s: float = 3600) -> int:
    import time

    removed = 0
    if not tmp_root.exists():
        return 0
    now = time.time()
    for entry in tmp_root.iterdir():
        if entry.name.startswith(prefix) and entry.is_dir() and now - entry.stat().st_mtime > max_age_s:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed
