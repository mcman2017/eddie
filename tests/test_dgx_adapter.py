"""dgx backend against a fake OpenAI-compatible server (offline MockTransport)."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from eddie.config import ConfigError, load_settings
from eddie.llm.errors import UnsupportedModalityError
from eddie.llm.gateway import EddieLLMGateway
from eddie.llm.types import EddieRequest
from tests.helpers import make_png

pytestmark = pytest.mark.anyio


def _settings(**extra):
    env = {**os.environ, "EDDIE_BACKEND": "dgx", "DGX_TEXT_BASE_URL": "http://dgx.invalid/v1",
           "DGX_VISION_BASE_URL": "http://dgx-vision.invalid/v1", "DGX_VISION_MODEL": "vision-model"}
    env.update(extra)
    return load_settings(env)


def _openai_reply(text="local answer", prompt=50, completion=20, reasoning=5):
    return {
        "id": "chatcmpl-1",
        "model": "gemma4-prod:20260910",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


async def test_text_request_maps_to_chat_completions_and_prices_shadow():
    seen = []
    settings = _settings()
    gw = EddieLLMGateway(settings, dgx_transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=_openai_reply())))
    req = EddieRequest.simple("triage this lemma", brain_region="R1.triage", effort="minimal",
                              max_output_tokens=128, instructions="be terse")
    resp = await gw.respond(req)
    sent = json.loads(seen[0].content)
    assert seen[0].url == "http://dgx.invalid/v1/chat/completions"
    assert sent["model"] == "gemma4-prod:20260910"
    assert sent["max_tokens"] == 128
    assert sent["reasoning_effort"] == "low"
    assert sent["messages"][0] == {"role": "system", "content": "be terse"}
    assert resp.text == "local answer"
    assert resp.cost_nano == 0
    assert resp.shadow_nano == 50 * 100 + 20 * 500
    assert not settings.ledger_path.exists()  # never touches the real ledger
    assert gw.governor.ledger.billable is False
    await gw.aclose()


async def test_images_route_to_the_vision_model():
    seen = []
    gw = EddieLLMGateway(_settings(), dgx_transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=_openai_reply())))
    req = EddieRequest.create(
        brain_region="R2.critic", effort="medium", max_output_tokens=256,
        messages=[{"role": "user", "content": [{"type": "text", "text": "is this plot blank?"},
                                               {"type": "image", "data": make_png(64, 64), "mime": "image/png"}]}],
    )
    await gw.respond(req)
    sent = json.loads(seen[0].content)
    assert seen[0].url.host == "dgx-vision.invalid"
    assert sent["model"] == "vision-model"
    assert sent["messages"][-1]["content"][1]["type"] == "image_url"
    await gw.aclose()


async def test_audio_is_refused_before_any_reservation():
    gw = EddieLLMGateway(_settings(), dgx_transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    req = EddieRequest.create(
        brain_region="R2.critic", effort="medium", max_output_tokens=256, allow_unbounded=True,
        messages=[{"role": "user", "content": [{"type": "audio", "data": b"RIFF", "format": "wav"}]}],
    )
    with pytest.raises(UnsupportedModalityError):
        await gw.respond(req)
    assert gw.governor.ledger.totals()["calls_by_state"] == {}
    await gw.aclose()


def test_unconfigured_dgx_refuses():
    with pytest.raises(ConfigError):
        EddieLLMGateway(load_settings({**os.environ, "EDDIE_BACKEND": "dgx"}))
