from __future__ import annotations

import json

import httpx
import pytest

from eddie.llm.telemetry import build_trace
from eddie.llm.types import EddieRequest
from tests.emberhelpers import make_gateway
from tests.helpers import ember_response

pytestmark = pytest.mark.anyio

FIELDS = {
    "timestamp", "response_id", "brain_region", "effort", "input_tokens", "cached_tokens",
    "output_tokens", "reasoning_tokens", "cost_usd", "cumulative_spend_usd",
}


async def test_trace_fields_and_monotonic_cumulative_spend():
    n = iter(range(100))
    gw, _, _ = make_gateway(lambda r: httpx.Response(200, json=ember_response(response_id=f"resp_{next(n)}")))
    gw.governor.ledger.add_adjustment(370_000_000, kind="opening_balance", note="dashboard")
    for region, effort, m in [("R1.triage", "minimal", 64), ("R2.critic", "medium", 512), ("R1.worker", "high", 1024)]:
        await gw.respond(EddieRequest.simple("hi", brain_region=region, effort=effort, max_output_tokens=m))
    trace = build_trace(gw.governor.ledger, lifetime_cap_nano=8_000_000_000)
    assert trace["schema"] == "eddie-ember-telemetry/1"
    assert len(trace["calls"]) == 3
    for call in trace["calls"]:
        assert FIELDS <= set(call)
        assert call["response_id"].startswith("resp_")
    cumulative = [c["cumulative_spend_usd"] for c in trace["calls"]]
    assert cumulative == sorted(cumulative)
    assert trace["calls"][0]["cumulative_spend_usd"] > 0.37  # includes the opening balance
    committed = gw.governor.ledger.committed_nano()
    assert trace["totals"]["total_spend_usd"] == round(committed / 1e9, 9)
    assert "shadow_calls" not in trace
    await gw.aclose()


async def test_status_snapshot_is_written_atomically():
    gw, _, _ = make_gateway(lambda r: httpx.Response(200, json=ember_response()))
    await gw.respond(EddieRequest.simple("hi", brain_region="R1.triage", effort="minimal", max_output_tokens=64))
    public = gw.settings.public_dir
    data = json.loads((public / "status.json").read_text())
    assert data["calls_by_state"] == {"settled": 1}
    assert data["lifetime_cap_usd"] == 8.0
    assert not [p for p in public.iterdir() if p.name.endswith(".tmp")]
    await gw.aclose()
