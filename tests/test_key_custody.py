"""The Ember key lives only inside the transport: never in os.environ, subprocesses or headers."""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

from eddie.config import ConfigError, load_settings
from eddie.llm.adapters.dgx import DgxAdapter
from eddie.llm.errors import LiveEmberDisabledError
from eddie.llm.gateway import EddieLLMGateway
from eddie.llm.types import EddieRequest
from tests.emberhelpers import ember_settings, prepare_live_state
from tests.helpers import ember_response

pytestmark = pytest.mark.anyio


async def test_env_key_is_removed_from_environ_and_only_the_transport_uses_it(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "env-key-value")
    settings = ember_settings()
    prepare_live_state(settings, key=None)
    seen = []
    gw = EddieLLMGateway(settings, ember_transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=ember_response())))
    assert "META_API_KEY" not in os.environ
    assert "authorization" not in {k.lower() for k in gw.adapter._client.headers}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import os; print(os.environ.get('META_API_KEY'))",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert stdout.decode().strip() == "None"
    await gw.respond(EddieRequest.simple("hi", brain_region="R1.triage", effort="minimal", max_output_tokens=32))
    assert seen[0].headers["authorization"] == "Bearer env-key-value"
    await gw.aclose()


def test_key_file_must_be_private():
    settings = ember_settings()
    prepare_live_state(settings, key="k", key_mode=0o644)
    with pytest.raises(LiveEmberDisabledError, match="600"):
        EddieLLMGateway(settings, ember_transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def test_missing_key_file_refuses():
    settings = ember_settings()
    prepare_live_state(settings, key=None)
    with pytest.raises(LiveEmberDisabledError, match="key file"):
        EddieLLMGateway(settings, ember_transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def test_mock_gateway_also_scrubs_env_key(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "should-vanish")
    EddieLLMGateway(load_settings(dict(os.environ)))
    assert "META_API_KEY" not in os.environ


@pytest.mark.parametrize("url", ["https://api.meta.ai/v1", "https://graph.meta.ai/v1", "https://meta.ai"])
def test_dgx_refuses_meta_hosts(url):
    settings = load_settings({**os.environ, "EDDIE_BACKEND": "dgx", "DGX_TEXT_BASE_URL": url})
    with pytest.raises(ConfigError):
        DgxAdapter(settings)


def test_key_provider_repr_never_shows_the_key(tmp_path):
    from eddie.llm.transport import KeyProvider

    kp = KeyProvider(tmp_path / "k", env={"META_API_KEY": "super-secret-value"})
    assert "super-secret-value" not in repr(kp)
