"""Filesystem layout.

The spend ledger, its anchor, the kill switch and the key live OUTSIDE the repo so that
no `make clean`, `git clean` or container volume can reset them. Everything rebuildable
(index, shadow ledger, artifacts, downloads) lives under the repo's git-ignored `data/`.

All helpers read HOME / XDG_* / EDDIE_DATA_DIR at call time, so tests can redirect them.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or home() / ".local" / "share")


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or home() / ".config")


def state_dir() -> Path:
    """Directory holding the canonical ledger, the kill switch and public snapshots."""
    return xdg_data_home() / "eddie"


def default_ledger_path() -> Path:
    return state_dir() / "ledger.sqlite"


def default_anchor_path() -> Path:
    return xdg_config_home() / "eddie" / "ledger-anchor.json"


def default_public_dir() -> Path:
    """Only directory any container may mount (read-only): status.json, telemetry.json."""
    return state_dir() / "public"


def default_kill_switch_path() -> Path:
    return state_dir() / "KILL"


def canonical_home() -> Path:
    """The passwd home, independent of $HOME, so a redirected HOME cannot hide an anchor.

    Tests point EDDIE_CANONICAL_HOME at their temp home.
    """
    return Path(os.environ.get("EDDIE_CANONICAL_HOME") or pwd.getpwuid(os.getuid()).pw_dir)


def canonical_anchor_path() -> Path:
    return canonical_home() / ".config" / "eddie" / "ledger-anchor.json"


def default_key_file() -> Path:
    return home() / ".secrets" / "eddie" / "meta_api_key"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.environ.get("EDDIE_DATA_DIR") or repo_root() / "data")


def default_shadow_ledger_path() -> Path:
    return data_dir() / "shadow.sqlite"


def default_index_path() -> Path:
    return data_dir() / "index" / "eddie_index.sqlite"


def default_artifact_dir() -> Path:
    return data_dir() / "artifacts"


def default_raw_dir() -> Path:
    return data_dir() / "raw"
