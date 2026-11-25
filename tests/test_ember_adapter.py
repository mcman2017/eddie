"""Ember adapter against an offline MockTransport: every settlement path, retries, concurrency."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from eddie.llm.errors import BudgetExceededError, CircuitOpenError, KillSwitchError, UpstreamError
from eddie.llm.pricing import reserve_nano
from eddie.llm.types import EddieRequest
from tests.emberhelpers import full_cost_usage, make_gateway
from tests.helpers import ember_response

pytestmark = pytest.mark.anyio


def ping(**kw):
    base = {"brain_region": "R1.triage", "effort": "minimal", "max_output_tokens": 64}
    base.update(kw)
    return EddieRequest.simple("hi", **base)


def states(gateway):
    return gateway.governor.ledger.totals()["calls_by_state"]


async def test_success_settles_exact_usage_cost():
    gw, rec, _ = make_gateway(lambda r: httpx.Response(200, json=ember_response(input_tokens=120, cached_tokens=20, output_tokens=40, reasoning_tokens=24, text="ready")))
    resp = await gw.respond(ping())
    assert resp.text == "ready"
    assert resp.response_id == "resp_test_0001"
    assert resp.reasoning_summary == ["considered the request"]
    expected = 100 * 100 + 20 * 4 + 40 * 500
    assert resp.cost_nano == expected
    assert gw.governor.ledger.committed_nano() == expected
    assert states(gw) == {"settled": 1}
    sent = rec.requests[0]
    assert sent.headers["authorization"] == "Bearer test-key-not-real"
    await gw.aclose()


async def test_429_retries_on_fresh_reservations_then_succeeds():
    replies = iter(
        [httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"code": "rate_limit_exceeded"}})] * 2
        + [httpx.Response(200, json=ember_response())]
    )
    gw, rec, sleeps = make_gateway(lambda r: next(replies))
    resp = await gw.respond(ping())
    assert resp.status == "completed"
    assert len(rec.requests) == 3
    assert len(sleeps) == 2 and all(2 <= s <= 2.5 for s in sleeps)
    assert states(gw) == {"released": 2, "settled": 1}
    await gw.aclose()


async def test_429_is_bounded():
    gw, rec, sleeps = make_gateway(lambda r: httpx.Response(429, json={"error": {}}))
    with pytest.raises(UpstreamError) as exc:
        await gw.respond(ping())
    assert exc.value.status == 429
    assert len(rec.requests) == 3
    assert states(gw) == {"released": 3}
    assert gw.governor.ledger.committed_nano() == 0
    await gw.aclose()


@pytest.mark.parametrize("status", [400, 404, 409, 500, 502, 503, 504])
async def test_ambiguous_failures_are_orphan_charged_and_not_retried(status):
    gw, rec, _ = make_gateway(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(UpstreamError):
        await gw.respond(ping())
    assert len(rec.requests) == 1
    assert states(gw) == {"orphan_charged": 1}
    row = gw.governor.ledger.charged_calls()[0]
    assert row["actual_nano"] == row["reserved_nano"]
    await gw.aclose()


async def test_read_timeout_is_charged_full_reservation():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    gw, _, _ = make_gateway(boom)
    with pytest.raises(UpstreamError):
        await gw.respond(ping())
    assert states(gw) == {"orphan_charged": 1}
    await gw.aclose()


async def test_connect_error_is_released():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    gw, _, _ = make_gateway(refuse)
    with pytest.raises(UpstreamError):
        await gw.respond(ping())
    assert states(gw) == {"released": 1}
    assert gw.governor.ledger.committed_nano() == 0
    await gw.aclose()


@pytest.mark.parametrize("status", [401, 402, 403])
async def test_auth_and_billing_errors_trip_the_breaker(status):
    gw, rec, _ = make_gateway(lambda r: httpx.Response(status, json={"error": {"type": "billing_error"}}))
    with pytest.raises(UpstreamError):
        await gw.respond(ping())
    assert states(gw) == {"released": 1}
    assert gw.governor.ledger.breaker()["state"] == "open"
    with pytest.raises(CircuitOpenError):
        await gw.respond(ping())
    assert len(rec.requests) == 1
    await gw.aclose()


async def test_200_without_usage_is_orphan_charged():
    body = ember_response(status="failed")
    body["usage"] = None
    gw, _, _ = make_gateway(lambda r: httpx.Response(200, json=body))
    resp = await gw.respond(ping())
    assert any("usage unusable" in w for w in resp.warnings)
    assert states(gw) == {"orphan_charged": 1}
    await gw.aclose()


async def test_openai_style_usage_charges_reservation_and_trips():
    body = ember_response(usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
    gw, _, _ = make_gateway(lambda r: httpx.Response(200, json=body))
    await gw.respond(ping())
    assert states(gw) == {"orphan_charged": 1}
    assert gw.governor.ledger.breaker()["state"] == "open"
    await gw.aclose()


async def test_input_above_estimate_charges_at_least_reservation_and_trips():
    gw, _, _ = make_gateway(lambda r: httpx.Response(200, json=ember_response(input_tokens=50_000)))
    resp = await gw.respond(ping())
    row = gw.governor.ledger.charged_calls()[0]
    assert row["actual_nano"] >= row["reserved_nano"]
    assert any("BREAKER TRIPPED" in w for w in resp.warnings)
    assert gw.governor.ledger.breaker()["state"] == "open"
    await gw.aclose()


async def test_extra_detail_counters_only_warn():
    body = ember_response()
    body["usage"]["input_tokens_details"]["image_tokens"] = 0
    gw, _, _ = make_gateway(lambda r: httpx.Response(200, json=body))
    resp = await gw.respond(ping())
    assert any("image_tokens" in w for w in resp.warnings)
    assert gw.governor.ledger.breaker()["state"] == "closed"
    await gw.aclose()


async def test_kill_switch_blocks_before_any_send():
    gw, rec, _ = make_gateway(lambda r: httpx.Response(200, json=ember_response()))
    gw.settings.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    gw.settings.kill_switch_path.touch()
    with pytest.raises(KillSwitchError):
        await gw.respond(ping())
    assert rec.requests == []
    await gw.aclose()


@pytest.mark.slow
async def test_fifty_concurrent_requests_through_the_gateway_respect_the_run_cap():
    seen = {"calls": 0}
    gw_holder = {}

    def handler(request):
        seen["calls"] += 1
        totals = gw_holder["gw"].governor.ledger.totals()
        assert totals["committed_nano"] + totals["inflight_reserved_nano"] <= 100_000_000
        return httpx.Response(200, json=ember_response(usage=full_cost_usage(request)))

    gw, rec, _ = make_gateway(handler)
    gw_holder["gw"] = gw
    req = EddieRequest.simple("x", brain_region="R1.worker", effort="high", max_output_tokens=16_384)
    per_call = reserve_nano(gw.preflight(req)[4].input_tokens, 16_384)

    async def one():
        try:
            await gw.respond(req)
            return "ok"
        except BudgetExceededError as exc:
            assert exc.kind == "run"
            return "refused"

    results = await asyncio.gather(*(one() for _ in range(50)))
    assert results.count("ok") == 100_000_000 // per_call == 12
    assert seen["calls"] == 12
    totals = gw.governor.ledger.totals()
    assert totals["inflight_count"] == 0
    assert totals["committed_nano"] <= 100_000_000
    await gw.aclose()
