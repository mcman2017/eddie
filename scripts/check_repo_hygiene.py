#!/usr/bin/env python3
"""Refuse commits that would publish licensed data, local state or secrets.

Stdlib only, so the git hook can run it with any python3.

    python3 scripts/check_repo_hygiene.py            # every tracked file (index + HEAD)
    python3 scripts/check_repo_hygiene.py --staged   # what the pending commit adds/changes
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 5 * 1024 * 1024

# Paths that must never be tracked (copyright/licensing compliance and local state).
FORBIDDEN_GLOBS = (
    "*.lean",
    "*.olean",
    "*.ilean",
    "*.tar*",
    "*.tgz",
    "*.zst",
    "*.partial",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite3",
    "*.db",
    "*.pem",
    "*.key",
    ".env",
    ".env.*",
    "initial-prompt.md",
    "CLAUDE.local.md",
    "eddie_ember_telemetry.json",
)
FORBIDDEN_DIR_PREFIXES = ("data/", "exports/", ".lake/", ".venv/", "node_modules/")
ALLOWED_EXCEPTIONS = {".env.example", "data/README.md"}

# Secret-looking content. Built from fragments so this file never matches itself.
_PRIV = "PRIVATE" + " KEY"
SECRET_PATTERNS = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*" + _PRIV + "-----"),
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]{24,}"),
    "inline Meta key": re.compile(r"META_API_KEY\s*=\s*['\"]?[A-Za-z0-9_\-\.]{12,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    "personal email": re.compile(
        r"[A-Za-z0-9._%+\-]+@(?:gmail|googlemail|yahoo|hotmail|outlook|icloud|proton)\.(?:com|me)",
        re.IGNORECASE,
    ),
}
CONTENT_SCAN_SKIP = {"scripts/check_repo_hygiene.py", "LICENSE"}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True
    ).stdout


def path_violations(path: str) -> list[str]:
    if path in ALLOWED_EXCEPTIONS:
        return []
    problems: list[str] = []
    name = path.rsplit("/", 1)[-1]
    for glob in FORBIDDEN_GLOBS:
        if fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(path, glob):
            problems.append(f"{path}: forbidden file pattern {glob!r}")
            break
    for prefix in FORBIDDEN_DIR_PREFIXES:
        if path.startswith(prefix) or f"/{prefix}" in path:
            problems.append(f"{path}: lives under forbidden directory {prefix!r}")
            break
    return problems


def content_violations(path: str, blob: bytes) -> list[str]:
    if len(blob) > MAX_FILE_BYTES:
        return [f"{path}: {len(blob)} bytes is larger than {MAX_FILE_BYTES} (data does not belong in git)"]
    if path in CONTENT_SCAN_SKIP or b"\0" in blob[:8192]:
        return []
    text = blob.decode("utf-8", errors="replace")
    return [
        f"{path}: looks like it contains a {label}"
        for label, pattern in SECRET_PATTERNS.items()
        if pattern.search(text)
    ]


def check(paths: list[str], read) -> list[str]:
    problems: list[str] = []
    for path in paths:
        problems += path_violations(path)
        blob = read(path)
        if blob is not None:
            problems += content_violations(path, blob)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="check the staged commit only")
    args = parser.parse_args(argv)

    if args.staged:
        paths = [p for p in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if p]

        def read(path: str) -> bytes | None:
            res = subprocess.run(["git", "-C", str(REPO), "show", f":{path}"], capture_output=True)
            return res.stdout if res.returncode == 0 else None
    else:
        paths = [p for p in _git("ls-files", "-z").split("\0") if p]

        def read(path: str) -> bytes | None:
            file = REPO / path
            return file.read_bytes() if file.is_file() else None

    problems = check(paths, read)
    if problems:
        print("repo hygiene check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
