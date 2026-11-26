"""Offline, $0 test harness.

Installed once per test process, before any test module is imported:

* an audit hook that refuses every non-loopback socket connect/DNS lookup, so no test can
  ever reach api.meta.ai or anything else on the network;
* an audit hook that refuses to open the REAL ledger, anchor, kill switch or key file
  (resolved from the passwd home, not from $HOME, so redirecting HOME cannot hide them).

And per test (autouse): HOME / XDG / data dirs point at a fresh temp dir, every key and
live-call variable is removed, and EDDIE_BACKEND is forced to "mock".
"""

from __future__ import annotations

import os
import pwd
import socket
import sys
from pathlib import Path

import pytest

_REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
_FORBIDDEN_PREFIXES = tuple(
    str(p)
    for p in (
        _REAL_HOME / ".local" / "share" / "eddie",
        _REAL_HOME / ".config" / "eddie",
        _REAL_HOME / ".secrets",
    )
)
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "", None}


class NetworkBlockedInTests(RuntimeError):
    pass


class RealStateTouchedInTests(RuntimeError):
    pass


def _address_is_loopback(address: object) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX path
        return True
    if isinstance(address, tuple) and address:
        return address[0] in _LOOPBACK_HOSTS
    return False


def _guard(event: str, args: tuple) -> None:
    if event == "socket.connect":
        sock, address = args[0], args[1]
        if getattr(sock, "family", None) == socket.AF_UNIX:
            return
        if not _address_is_loopback(address):
            raise NetworkBlockedInTests(f"network access blocked in tests: {address!r}")
    elif event == "socket.getaddrinfo":
        host = args[0]
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        if host not in _LOOPBACK_HOSTS:
            raise NetworkBlockedInTests(f"DNS lookup blocked in tests: {host!r}")
    elif event in ("open", "sqlite3.connect"):
        target = args[0]
        if isinstance(target, int):
            return
        if isinstance(target, bytes):
            target = target.decode(errors="replace")
        if isinstance(target, (str, os.PathLike)):
            text = os.fspath(target)
            if text.startswith(_FORBIDDEN_PREFIXES):
                raise RealStateTouchedInTests(f"tests must never open real state: {text}")


sys.addaudithook(_guard)

_SCRUBBED_ENV = (
    "META_API_KEY",
    "META_API_KEY_FILE",
    "EDDIE_KEY_FD",
    "EDDIE_ALLOW_LIVE_EMBER",
    "EDDIE_RUN_CAP_USD",
    "EDDIE_RUN_CAP_CONFIRM",
    "EDDIE_DAILY_CAP_USD",
    "EDDIE_DAILY_CAP_CONFIRM",
    "EDDIE_LIFETIME_CAP_USD",
    "EDDIE_LEDGER_PATH",
    "EDDIE_PUBLIC_DIR",
    "EDDIE_SHADOW_LEDGER_PATH",
    "EDDIE_INDEX_PATH",
    "EDDIE_LANDSCAPE_DIR",
    "EDDIE_MAX_CONCURRENCY",
    "DGX_TEXT_BASE_URL",
    "DGX_VISION_BASE_URL",
    "DGX_API_KEY_FILE",
    "HF_TOKEN",
)


@pytest.fixture(autouse=True)
def offline_sandboxed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for key in _SCRUBBED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("EDDIE_CANONICAL_HOME", str(home))
    monkeypatch.setenv("EDDIE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EDDIE_EXPORTS_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("EDDIE_BACKEND", "mock")
    return home


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
