"""Build an Ember-mode gateway whose transport is an offline httpx.MockTransport."""

from __future__ import annotations

import json
import os

import httpx

from eddie.config import load_settings
from eddie.llm.estimate import estimate_body
from eddie.llm.gateway import EddieLLMGateway
from eddie.llm.ledger import initialize_canonical_ledger

TEST_KEY = "test-key-not-real"


def ember_settings(extra_env: dict | None = None):
    env = dict(os.environ)
    env.update({"EDDIE_BACKEND": "ember", "EDDIE_ALLOW_LIVE_EMBER": "true"})
    env.update(extra_env or {})
    return load_settings(env)


def prepare_live_state(settings, *, anchored: bool = True, key: str | None = TEST_KEY, key_mode: int = 0o600):
    if anchored and not settings.ledger_path.exists():
        initialize_canonical_ledger(
            settings.ledger_path,
            settings.anchor_path,
            lifetime_cap_nano=settings.lifetime_cap_nano,
            opening_balance_nano=0,
        )
    if key is not None:
        settings.key_file.parent.mkdir(parents=True, exist_ok=True)
        settings.key_file.write_text(key)
        os.chmod(settings.key_file, key_mode)


class Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def make_gateway(handler, *, extra_env: dict | None = None, anchored: bool = True, key: str | None = TEST_KEY):
    settings = ember_settings(extra_env)
    prepare_live_state(settings, anchored=anchored, key=key)
    recorder = Recorder(handler)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    gateway = EddieLLMGateway(settings, ember_transport=httpx.MockTransport(recorder), sleep=fake_sleep)
    return gateway, recorder, sleeps


def full_cost_usage(request: httpx.Request) -> dict:
    """Usage that bills exactly the reservation: input = upper-bound estimate, output = max."""
    body = json.loads(request.content)
    est = estimate_body(body).input_tokens
    m = body["max_output_tokens"]
    return {
        "input_tokens": est,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": m,
        "output_tokens_details": {"reasoning_tokens": m // 2},
        "total_tokens": est + m,
    }
