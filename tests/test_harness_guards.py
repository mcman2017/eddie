"""The test harness itself must make network access and real-state access impossible."""

import os
import pwd
import socket
from pathlib import Path

import pytest

from tests.conftest import NetworkBlockedInTests, RealStateTouchedInTests


def test_dns_lookups_are_blocked():
    with pytest.raises(NetworkBlockedInTests):
        socket.getaddrinfo("api.meta.ai", 443)


def test_non_loopback_connect_is_blocked():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlockedInTests):
            s.connect(("192.0.2.1", 443))  # TEST-NET-1, never routable
    finally:
        s.close()


def test_real_ledger_paths_cannot_be_opened():
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    with pytest.raises(RealStateTouchedInTests):
        open(real_home / ".local" / "share" / "eddie" / "ledger.sqlite", "rb")
    with pytest.raises(RealStateTouchedInTests):
        open(real_home / ".secrets" / "eddie" / "meta_api_key", "rb")


def test_env_is_scrubbed_and_redirected(offline_sandboxed_env):
    assert "META_API_KEY" not in os.environ
    assert os.environ["EDDIE_BACKEND"] == "mock"
    assert os.environ["HOME"] == str(offline_sandboxed_env)
