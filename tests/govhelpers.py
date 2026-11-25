"""Helpers shared by governor/ledger tests (importable by spawn-started children)."""

from __future__ import annotations

import time
from pathlib import Path

from eddie.config import load_settings
from eddie.llm import ledger as ledger_mod
from eddie.llm.estimate import Estimate
from eddie.llm.governor import SpendGovernor
from eddie.llm.ledger import Caps, Ledger, initialize_canonical_ledger
from eddie.llm.pricing import reserve_nano, worst_case_w_nano


def billable_governor(extra_env: dict | None = None, *, lifetime_cap_nano: int = 8_000_000_000, opening_nano: int = 0):
    import os

    env = dict(os.environ)
    env.update(extra_env or {})
    settings = load_settings(env)
    initialize_canonical_ledger(
        settings.ledger_path,
        settings.anchor_path,
        lifetime_cap_nano=lifetime_cap_nano,
        opening_balance_nano=opening_nano,
    )
    return SpendGovernor(settings, billable=True)


def fixed_estimate(tokens: int) -> Estimate:
    return Estimate(input_tokens=tokens, text_tokens=tokens - 1_024, image_tokens=0)


def exact_usage(est_input: int, max_output: int) -> dict:
    """A usage block that costs exactly the reservation (worst case is billed)."""
    return {
        "input_tokens": est_input,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": max_output,
        "output_tokens_details": {"reasoning_tokens": max_output // 2},
        "total_tokens": est_input + max_output,
    }


def child_reserve_loop(args: tuple) -> dict:
    """Run in a separate process: hammer one ledger with reserve -> hold -> settle."""
    path, attempts, est, m, hold_s, caps_tuple, gate_delay = args
    if gate_delay is not None:
        ledger_mod._GATE_DELAY_S = gate_delay
    ledger = Ledger(Path(path), billable=True)
    import uuid

    run_id = uuid.uuid4().hex
    caps = Caps(*caps_tuple)
    ledger.register_run(run_id, caps.run_nano)
    ok = refused = 0
    reserved = reserve_nano(est, m)
    for _ in range(attempts):
        try:
            call_id = ledger.reserve(
                run_id=run_id,
                backend="ember",
                model="muse-ember-1.0",
                region="R1.worker",
                effort="high",
                tag="race",
                body_sha256="0" * 64,
                est_input=est,
                max_output=m,
                bounded=True,
                reserved_nano=reserved,
                w_nano=worst_case_w_nano(m),
                caps=caps,
            )
        except Exception as exc:  # noqa: BLE001 - counted, type checked by the parent
            if type(exc).__name__ != "BudgetExceededError":
                raise
            refused += 1
            continue
        ok += 1
        ledger.mark_dispatched(call_id)
        time.sleep(hold_s)
        ledger.settle(
            call_id,
            actual_nano=reserved,
            shadow_nano=reserved,
            input_tokens=est,
            cached_tokens=0,
            output_tokens=m,
            reasoning_tokens=0,
            response_id=f"resp_{call_id[:8]}",
            status="completed",
            http_status=200,
            latency_ms=int(hold_s * 1000),
        )
    return {"ok": ok, "refused": refused}


def child_reserve_and_hang(args: tuple) -> None:
    """Reserve (and optionally dispatch), then hang until SIGKILLed by the parent."""
    path, dispatch, ready_path = args
    import uuid

    ledger = Ledger(Path(path), billable=True)
    run_id = uuid.uuid4().hex
    ledger.register_run(run_id, 100_000_000)
    call_id = ledger.reserve(
        run_id=run_id,
        backend="ember",
        model="muse-ember-1.0",
        region="R1.triage",
        effort="minimal",
        tag="crash",
        body_sha256="0" * 64,
        est_input=2_000,
        max_output=64,
        bounded=True,
        reserved_nano=reserve_nano(2_000, 64),
        w_nano=worst_case_w_nano(64),
        caps=Caps(8_000_000_000, 2_000_000_000, 100_000_000, 4),
    )
    if dispatch:
        ledger.mark_dispatched(call_id)
    Path(ready_path).write_text(call_id)
    time.sleep(600)
