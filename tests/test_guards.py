"""Every guard fires BEFORE any network I/O (the fake transport must never be reached)."""

from __future__ import annotations

import json

import httpx
import pytest

from eddie.llm.body import build_body, canonical_bytes, sha256_hex
from eddie.llm.errors import (
    BudgetExceededError,
    ForbiddenParameterError,
    HardInputTokenLimitError,
    LedgerIntegrityError,
    LiveEmberDisabledError,
    MissingParameterError,
    TransportGuardError,
    UnboundedInputError,
)
from eddie.llm.gateway import EddieLLMGateway
from eddie.llm.ledger import Ledger
from eddie.llm.pricing import reserve_nano, worst_case_w_nano
from eddie.llm.transport import EMBER_URL, RESERVATION_EXTENSION, GovernedTransport, KeyProvider
from eddie.llm.types import EddieRequest
from tests.emberhelpers import ember_settings, make_gateway, prepare_live_state
from tests.helpers import ember_response

pytestmark = pytest.mark.anyio

OK = lambda r: httpx.Response(200, json=ember_response())  # noqa: E731


def _ping(**kw):
    base = {"brain_region": "R1.triage", "effort": "minimal", "max_output_tokens": 64}
    base.update(kw)
    return base


async def _assert_refused_before_io(request, exc_type):
    gw, rec, _ = make_gateway(OK)
    with pytest.raises(exc_type):
        await gw.respond(request)
    assert rec.requests == []
    assert gw.governor.ledger.totals()["calls_by_state"] == {}
    await gw.aclose()


async def test_input_over_190k_is_refused():
    await _assert_refused_before_io(EddieRequest.simple("x" * 190_000, **_ping()), HardInputTokenLimitError)


async def test_effort_missing_or_none():
    req = {"brain_region": "R1.triage", "max_output_tokens": 64, "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    await _assert_refused_before_io(req, MissingParameterError)
    await _assert_refused_before_io({**req, "effort": "none"}, ForbiddenParameterError)


async def test_max_output_missing_or_too_small():
    req = {"brain_region": "R1.triage", "effort": "minimal", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    await _assert_refused_before_io(req, MissingParameterError)
    await _assert_refused_before_io({**req, "max_output_tokens": 8}, ForbiddenParameterError)


async def test_region_effort_mismatch():
    await _assert_refused_before_io(EddieRequest.simple("hi", **_ping(effort="high")), ForbiddenParameterError)


async def test_forbidden_keys_in_dict_requests():
    req = {**_ping(), "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    await _assert_refused_before_io({**req, "tools": [{"type": "web_search"}]}, ForbiddenParameterError)
    await _assert_refused_before_io({**req, "previous_response_id": "resp_1"}, ForbiddenParameterError)


async def test_unbounded_parts_need_explicit_opt_in_and_hit_the_run_cap():
    audio = {"type": "audio", "data": b"RIFF0000WAVE", "format": "wav"}
    req = {"brain_region": "R2.critic", "effort": "medium", "max_output_tokens": 256,
           "messages": [{"role": "user", "content": [{"type": "text", "text": "listen"}, audio]}]}
    await _assert_refused_before_io(req, UnboundedInputError)
    # opted in: reserves W(m) ~ $0.31, which the default $0.10 run cap refuses
    await _assert_refused_before_io({**req, "allow_unbounded": True}, BudgetExceededError)


async def test_temperature_and_text_are_allowed_and_text_counts_toward_estimate():
    gw, rec, _ = make_gateway(OK)
    plain = EddieRequest.simple("hi", **_ping())
    schema = {"format": {"type": "json_schema", "name": "verdict", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}
    structured = EddieRequest.simple("hi", **_ping(), temperature=0.1, text_format=schema)
    assert gw.preflight(structured)[4].input_tokens > gw.preflight(plain)[4].input_tokens
    await gw.respond(structured)
    sent = json.loads(rec.requests[0].content)
    assert sent["temperature"] == 0.1 and sent["text"] == schema and sent["store"] is False
    await gw.aclose()


# --- gateway construction guards ----------------------------------------------------------


@pytest.mark.parametrize("allow", ["false", "true"])
def test_a_refused_ember_gateway_leaves_no_ledger_behind(allow):
    """Regression: a refusal must not create the real ledger file (it would block `budget init`)."""
    settings = ember_settings({"EDDIE_ALLOW_LIVE_EMBER": allow})
    with pytest.raises(LiveEmberDisabledError):
        EddieLLMGateway(settings)  # real network under pytest, or flag absent
    assert not settings.ledger_path.exists()
    assert not settings.ledger_path.parent.exists()


def test_live_flag_absent_refuses_to_build():
    settings = ember_settings({"EDDIE_ALLOW_LIVE_EMBER": "false"})
    prepare_live_state(settings)
    with pytest.raises(LiveEmberDisabledError):
        EddieLLMGateway(settings, ember_transport=httpx.MockTransport(OK))


def test_unanchored_ledger_refuses_to_build():
    settings = ember_settings()
    prepare_live_state(settings, anchored=False)
    with pytest.raises(LedgerIntegrityError):
        EddieLLMGateway(settings, ember_transport=httpx.MockTransport(OK))


def test_real_network_gateway_is_impossible_under_pytest(monkeypatch):
    settings = ember_settings()
    prepare_live_state(settings)
    with pytest.raises(LiveEmberDisabledError, match="pytest"):
        EddieLLMGateway(settings)
    # import-time construction: PYTEST_CURRENT_TEST not set yet, still refused
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(LiveEmberDisabledError, match="pytest"):
        EddieLLMGateway(settings)


def test_real_network_transport_is_impossible_under_pytest(tmp_path):
    led = Ledger(tmp_path / "l.sqlite", billable=True)
    with pytest.raises(LiveEmberDisabledError):
        GovernedTransport(led, KeyProvider(tmp_path / "k", env={}), kill_switch=tmp_path / "KILL")


# --- the wire-level transport ---------------------------------------------------------------


@pytest.fixture
def wire(tmp_path):
    led = Ledger(tmp_path / "wire.sqlite", billable=True)
    led.register_run("run", 10**12)
    key = tmp_path / "key"
    key.write_text("k")
    key.chmod(0o600)
    sends = []
    inner = httpx.MockTransport(lambda r: sends.append(r) or httpx.Response(200, json=ember_response()))
    transport = GovernedTransport(led, KeyProvider(key, env={}), kill_switch=tmp_path / "KILL", inner=inner)
    return led, transport, sends, tmp_path


def _reserved_body(led, **req_kw):
    req = EddieRequest.simple("hi", **_ping(**req_kw))
    body = build_body(req)
    data = canonical_bytes(body)
    from eddie.llm.estimate import estimate_body

    est = estimate_body(body).input_tokens
    m = body["max_output_tokens"]
    cid = led.reserve(
        run_id="run", backend="ember", model="muse-ember-1.0", region=req.brain_region, effort=req.effort,
        tag=None, body_sha256=sha256_hex(data), est_input=est, max_output=m, bounded=True,
        reserved_nano=reserve_nano(est, m), w_nano=worst_case_w_nano(m),
        caps=__import__("eddie.llm.ledger", fromlist=["Caps"]).Caps(8 * 10**9, 2 * 10**9, 10**12, 4),
    )
    led.mark_dispatched(cid)
    return cid, body, data


async def _send(transport, data, cid=None, url=EMBER_URL, method="POST"):
    ext = {RESERVATION_EXTENSION: cid} if cid else {}
    async with httpx.AsyncClient(transport=transport) as client:
        return await client.request(method, url, content=data, extensions=ext)


async def test_transport_happy_path_attaches_key_once(wire):
    led, transport, sends, _ = wire
    cid, _, data = _reserved_body(led)
    r = await _send(transport, data, cid)
    assert r.status_code == 200
    assert sends[0].headers["authorization"] == "Bearer k"
    with pytest.raises(TransportGuardError, match="already used"):
        await _send(transport, data, cid)
    assert len(sends) == 1


async def test_transport_refuses_without_reservation(wire):
    led, transport, sends, _ = wire
    _, _, data = _reserved_body(led)
    with pytest.raises(TransportGuardError, match="without a reservation"):
        await _send(transport, data)
    assert sends == []


@pytest.mark.parametrize(
    "url,method",
    [
        ("https://example.com/v1/responses", "POST"),
        ("https://api.meta.ai/v1/chat/completions", "POST"),
        ("https://api.meta.ai/v1/responses/input_tokens", "POST"),
        ("http://api.meta.ai/v1/responses", "POST"),
        ("https://api.meta.ai/v1/responses", "GET"),
        ("https://api.meta.ai/v1/responses?x=1", "POST"),
    ],
)
async def test_transport_refuses_other_endpoints(wire, url, method):
    led, transport, sends, _ = wire
    cid, _, data = _reserved_body(led)
    with pytest.raises(TransportGuardError):
        await _send(transport, data, cid, url=url, method=method)
    assert sends == []


async def test_transport_refuses_body_changed_after_reservation(wire):
    led, transport, sends, _ = wire
    cid, body, _ = _reserved_body(led)
    body["input"][0]["content"][0]["text"] = "something much longer than before"
    with pytest.raises(TransportGuardError, match="body changed"):
        await _send(transport, canonical_bytes(body), cid)
    assert sends == []


@pytest.mark.parametrize("mutation", ["store_true", "store_missing", "tools", "effort"])
async def test_transport_refuses_schema_violations(wire, mutation):
    led, transport, sends, _ = wire
    cid, body, _ = _reserved_body(led)
    if mutation == "store_true":
        body["store"] = True
    elif mutation == "store_missing":
        del body["store"]
    elif mutation == "tools":
        body["tools"] = [{"type": "web_search"}]
    else:
        body["reasoning"]["effort"] = "max"
    with pytest.raises(TransportGuardError):
        await _send(transport, canonical_bytes(body), cid)
    assert sends == []


async def test_transport_honours_kill_switch(wire):
    led, transport, sends, tmp = wire
    cid, _, data = _reserved_body(led)
    (tmp / "KILL").touch()
    with pytest.raises(TransportGuardError, match="kill switch"):
        await _send(transport, data, cid)
    assert sends == []
