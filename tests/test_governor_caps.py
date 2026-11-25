"""Overspend races: async tasks, threads, fork and spawn processes, all against real SQLite."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import threading

import pytest

from eddie.llm import ledger as ledger_mod
from eddie.llm.errors import BudgetExceededError
from eddie.llm.ledger import Caps, Ledger
from eddie.llm.pricing import reserve_nano, worst_case_w_nano
from tests.govhelpers import billable_governor, child_reserve_loop, exact_usage, fixed_estimate

EST = 1_000
M = 16_384  # R1.worker ceiling: each reservation ~ $0.0083, so 12 fit in $0.10
PER_CALL = reserve_nano(EST, M)
RUN_CAP = 100_000_000


@pytest.mark.anyio
async def test_fifty_async_workers_cannot_overspend_the_run_cap():
    gov = billable_governor()
    assert gov.caps.run_nano == RUN_CAP
    sem = asyncio.Semaphore(gov.caps.max_concurrency)
    billed = {"total": 0, "sends": 0}
    lock = threading.Lock()

    async def fake_send(res):
        # Invariants checked on EVERY send: the ledger already accounts for everything
        # billed (including this call), and nothing exceeds the run cap.
        totals = await asyncio.to_thread(gov.ledger.totals)
        with lock:
            billed["total"] += res.reserved_nano
            billed["sends"] += 1
            assert billed["total"] <= totals["committed_nano"] + totals["inflight_reserved_nano"]
            assert totals["committed_nano"] + totals["inflight_reserved_nano"] <= RUN_CAP
        await asyncio.sleep(0.02)
        return {"id": f"resp_{res.call_id[:8]}", "status": "completed", "usage": exact_usage(EST, M)}

    async def worker(i):
        async with sem:
            try:
                res = await asyncio.to_thread(
                    gov.reserve,
                    backend="ember",
                    region="R1.worker",
                    effort="high",
                    max_output=M,
                    estimate=fixed_estimate(EST),
                    body_sha256=f"{i:064x}",
                    allow_unbounded=False,
                )
            except BudgetExceededError as exc:
                assert exc.kind == "run"
                return "refused"
            await asyncio.to_thread(gov.dispatch, res)
            payload = await fake_send(res)
            await asyncio.to_thread(gov.settle_response, res, payload, http_status=200, latency_ms=20)
            return "ok"

    results = await asyncio.gather(*(worker(i) for i in range(50)))
    assert results.count("ok") == RUN_CAP // PER_CALL == 12
    assert billed["sends"] == 12
    totals = gov.ledger.totals()
    assert totals["inflight_count"] == 0  # zero leak
    assert totals["committed_nano"] == 12 * PER_CALL <= RUN_CAP


def _race_with_threads(ledger_path, caps, n_threads, attempts):
    results = []

    def run():
        results.append(child_reserve_loop((str(ledger_path), attempts, EST, M, 0.0, caps, None)))

    threads = [threading.Thread(target=run) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["threads", "fork", "spawn"])
def test_injected_gate_delay_still_serializes_reservations(mode, tmp_path, monkeypatch):
    """A 50 ms sleep between the cap SUMs and the INSERT would let broken locking overspend."""
    monkeypatch.setattr(ledger_mod, "_GATE_DELAY_S", 0.05 if mode == "threads" else 0.0)
    path = tmp_path / f"race-{mode}.sqlite"
    Ledger(path, billable=True)
    # A daily cap is the binding cap here: shared by every process, unlike run caps.
    daily_cap = 5 * PER_CALL + PER_CALL // 2
    caps = (8_000_000_000, daily_cap, 10**12, 16)
    if mode == "threads":
        results = _race_with_threads(path, caps, 4, 4)
    else:
        ctx = mp.get_context(mode)
        with ctx.Pool(4) as pool:
            results = pool.map(child_reserve_loop, [(str(path), 4, EST, M, 0.0, caps, 0.05)] * 4)
    ok = sum(r["ok"] for r in results)
    assert ok == 5, results
    led = Ledger(path, billable=True)
    assert led.committed_nano() == 5 * PER_CALL <= daily_cap


@pytest.mark.slow
def test_lifetime_worst_case_gate_across_four_processes(tmp_path):
    """committed = cap - 2.5 W  =>  never more than 2 calls in flight anywhere."""
    path = tmp_path / "lifetime.sqlite"
    led = Ledger(path, billable=True)
    m = 16
    w = worst_case_w_nano(m)
    cap = 8_000_000_000
    led.add_adjustment(cap - (5 * w) // 2, kind="manual", note="near the cap")
    caps = (cap, 10**12, 10**12, 16)
    ctx = mp.get_context("spawn")
    with ctx.Pool(4) as pool:
        results = pool.map(child_reserve_loop, [(str(path), 3, 200, m, 0.15, caps, 0.0)] * 4)
    assert sum(r["refused"] for r in results) > 0
    with led.read() as con:
        rows = con.execute(
            "SELECT created_at, finished_at, gate_total_nano FROM calls ORDER BY created_at"
        ).fetchall()
    assert rows, "some calls must have been admitted"
    assert all(r["gate_total_nano"] <= cap for r in rows)
    # interval overlap: never more than 2 reservations alive at the same instant
    events = sorted([(r["created_at"], 1) for r in rows] + [(r["finished_at"], -1) for r in rows])
    alive = peak = 0
    for _, delta in events:
        alive += delta
        peak = max(peak, alive)
    assert peak <= 2


def test_daily_cap_blocks(tmp_path):
    led = Ledger(tmp_path / "daily.sqlite", billable=True)
    led.register_run("r1", RUN_CAP)
    caps = Caps(8_000_000_000, PER_CALL, 10**12, 4)
    common = dict(
        run_id="r1", backend="ember", model="muse-ember-1.0", region="R1.worker", effort="high",
        tag=None, body_sha256="0" * 64, est_input=EST, max_output=M, bounded=True,
        reserved_nano=PER_CALL, w_nano=worst_case_w_nano(M), caps=caps,
    )
    led.reserve(**common)
    with pytest.raises(BudgetExceededError) as exc:
        led.reserve(**common)
    assert exc.value.kind == "daily"


def test_concurrency_cap_is_global(tmp_path):
    led = Ledger(tmp_path / "conc.sqlite", billable=True)
    caps = Caps(8_000_000_000, 10**12, 10**12, 2)
    for run in ("a", "b", "c"):
        led.register_run(run, 10**12)
    common = dict(
        backend="ember", model="muse-ember-1.0", region="R1.triage", effort="minimal", tag=None,
        body_sha256="0" * 64, est_input=1_100, max_output=16, bounded=True,
        reserved_nano=reserve_nano(1_100, 16), w_nano=worst_case_w_nano(16), caps=caps,
    )
    led.reserve(run_id="a", **common)
    led.reserve(run_id="b", **common)
    with pytest.raises(BudgetExceededError) as exc:
        led.reserve(run_id="c", **common)
    assert exc.value.kind == "concurrency"


def test_lifetime_gate_uses_worst_case_not_estimate(tmp_path):
    led = Ledger(tmp_path / "w.sqlite", billable=True)
    led.register_run("r", 10**12)
    w = worst_case_w_nano(16)
    led.add_adjustment(8_000_000_000 - w + 1, kind="manual")  # one W no longer fits
    with pytest.raises(BudgetExceededError) as exc:
        led.reserve(
            run_id="r", backend="ember", model="muse-ember-1.0", region="R1.triage",
            effort="minimal", tag=None, body_sha256="0" * 64, est_input=1_100, max_output=16,
            bounded=True, reserved_nano=reserve_nano(1_100, 16), w_nano=w,
            caps=Caps(8_000_000_000, 10**12, 10**12, 4),
        )
    assert exc.value.kind == "lifetime"


def test_gate_delay_hook_defaults_off():
    assert ledger_mod._GATE_DELAY_S == 0.0
