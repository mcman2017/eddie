"""Ledger state machine, CHECK constraints, orphan sweep, crash drills and anchoring."""

from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import signal
import sqlite3
import time
from pathlib import Path

import pytest

from eddie.config import RESERVATION_TTL_S, load_settings
from eddie.llm import governor as governor_mod
from eddie.llm import ledger as ledger_mod
from eddie.llm.errors import (
    CircuitOpenError,
    KillSwitchError,
    LedgerIntegrityError,
    LiveEmberDisabledError,
    TransportGuardError,
)
from eddie.llm.governor import SpendGovernor, filesystem_type, running_under_pytest
from eddie.llm.ledger import Caps, Ledger, initialize_canonical_ledger
from eddie.llm.pricing import reserve_nano, worst_case_w_nano
from tests.govhelpers import billable_governor, child_reserve_and_hang, exact_usage, fixed_estimate

CAPS = Caps(8_000_000_000, 2_000_000_000, 100_000_000, 4)


def _reserve(led: Ledger, run_id: str = "run", *, now: float | None = None, m: int = 64) -> str:
    return led.reserve(
        run_id=run_id, backend="ember", model="muse-ember-1.0", region="R1.triage", effort="minimal",
        tag=None, body_sha256="a" * 64, est_input=2_000, max_output=m, bounded=True,
        reserved_nano=reserve_nano(2_000, m), w_nano=worst_case_w_nano(m), caps=CAPS, now=now,
    )


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", billable=True)
    ledger.register_run("run", 100_000_000)
    return ledger


def test_cas_transitions(led):
    cid = _reserve(led)
    led.mark_dispatched(cid)
    with pytest.raises(LedgerIntegrityError):
        led.mark_dispatched(cid)  # not 'reserved' any more
    led.claim_send(cid, "a" * 64)
    with pytest.raises(TransportGuardError):
        led.claim_send(cid, "a" * 64)  # single use
    led.settle(cid, actual_nano=100, shadow_nano=100, input_tokens=10, cached_tokens=0, output_tokens=5,
               reasoning_tokens=0, response_id="resp_1", status="completed", http_status=200, latency_ms=5)
    with pytest.raises(LedgerIntegrityError):
        led.release(cid, http_status=None, error="late")
    assert led.get_call(cid)["state"] == "settled"


def test_claim_send_rejects_changed_body(led):
    cid = _reserve(led)
    led.mark_dispatched(cid)
    with pytest.raises(TransportGuardError, match="body changed"):
        led.claim_send(cid, "b" * 64)


def test_claim_requires_dispatch(led):
    cid = _reserve(led)
    with pytest.raises(TransportGuardError):
        led.claim_send(cid, "a" * 64)


def test_check_constraints_reject_inconsistent_rows(led):
    cid = _reserve(led)
    with led.tx() as con, pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE calls SET state = 'settled' WHERE id = ?", (cid,))  # no actual_nano
    with led.tx() as con, pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE calls SET state = 'orphan_charged', actual_nano = 1 WHERE id = ?", (cid,))
    with led.tx() as con, pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE calls SET reserved_nano = -5 WHERE id = ?", (cid,))
    with pytest.raises(ValueError):
        led.add_adjustment(-1, kind="manual")


def test_orphan_charge_is_never_refunded(led):
    cid = _reserve(led)
    led.mark_dispatched(cid)
    led.orphan_charge(cid, http_status=504, error="gateway timeout")
    reserved = led.get_call(cid)["reserved_nano"]
    assert led.committed_nano() == reserved
    # a late response with a smaller real cost does not lower committed spend
    led.settle(cid, actual_nano=10, shadow_nano=10, input_tokens=5, cached_tokens=0, output_tokens=5,
               reasoning_tokens=0, response_id="resp_late", status="completed", http_status=200, latency_ms=1)
    assert led.committed_nano() == reserved


def test_orphan_charge_of_unsent_reservation_releases_it(led):
    cid = _reserve(led)
    led.orphan_charge(cid, http_status=None, error="never sent")
    assert led.get_call(cid)["state"] == "released"
    assert led.committed_nano() == 0


def test_ttl_sweep_releases_reserved_and_charges_dispatched(led):
    old = time.time() - RESERVATION_TTL_S - 5
    a = _reserve(led, now=old)
    b = _reserve(led, now=old)
    led.mark_dispatched(b)
    led.sweep()
    assert led.get_call(a)["state"] == "released"
    assert led.get_call(b)["state"] == "orphan_charged"
    assert led.committed_nano() == led.get_call(b)["reserved_nano"]


def test_clock_jump_backwards_keeps_reservations_counted(led):
    future = time.time() + 5_000
    cid = _reserve(led, now=future)
    led.sweep()
    assert led.get_call(cid)["state"] == "reserved"
    assert led.totals()["inflight_worst_case_nano"] == worst_case_w_nano(64)


@pytest.mark.slow
@pytest.mark.parametrize("dispatch", [False, True])
def test_crash_drill_sigkill(tmp_path, dispatch):
    path = tmp_path / "crash.sqlite"
    Ledger(path, billable=True)
    ready = tmp_path / "ready"
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=child_reserve_and_hang, args=((str(path), dispatch, str(ready)),))
    proc.start()
    deadline = time.time() + 30
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    call_id = ready.read_text()
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(10)

    led = Ledger(path, billable=True)
    led.sweep()  # same boot + pid namespace, dead pid: swept immediately (no TTL wait)
    row = led.get_call(call_id)
    if dispatch:
        # it may have been billed: the full reservation is committed (>= anything billed)
        assert row["state"] == "orphan_charged"
        assert led.committed_nano() == row["reserved_nano"]
    else:
        assert row["state"] == "released"
        assert led.committed_nano() == 0


def test_breaker_blocks_until_manual_reset(led):
    led.trip("manual test")
    with pytest.raises(CircuitOpenError):
        _reserve(led)
    led.reset_breaker()
    _reserve(led)


def test_consecutive_failures_trip_the_breaker(led):
    for _ in range(5):
        cid = _reserve(led)
        led.mark_dispatched(cid)
        led.orphan_charge(cid, http_status=500, error="upstream 500")
    assert led.breaker()["state"] == "open"


def test_kill_switch_blocks_reservations(led, tmp_path):
    kill = tmp_path / "KILL"
    kill.touch()
    with pytest.raises(KillSwitchError):
        led.reserve(
            run_id="run", backend="ember", model="muse-ember-1.0", region="R1.triage", effort="minimal",
            tag=None, body_sha256="a" * 64, est_input=2_000, max_output=64, bounded=True,
            reserved_nano=1, w_nano=worst_case_w_nano(64), caps=CAPS, kill_switch=kill,
        )


def test_persisted_lifetime_cap_wins_when_lower(led):
    led.set_lifetime_cap(worst_case_w_nano(64) - 1)
    with pytest.raises(Exception, match="lifetime"):
        _reserve(led)


# --- anchoring -------------------------------------------------------------------------------


def _settings():
    return load_settings(dict(os.environ))


def test_init_writes_anchor_and_verifies():
    s = _settings()
    led = initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=8_000_000_000,
                                      opening_balance_nano=370_000_000, note="dashboard")
    anchor = led.verify_anchor(s.anchor_path)
    assert anchor.high_water_nano == 370_000_000
    assert led.committed_nano() == 370_000_000
    assert oct(os.stat(s.anchor_path).st_mode & 0o777) == "0o600"


def test_init_refuses_second_ledger_and_existing_anchor(tmp_path):
    s = _settings()
    initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1, opening_balance_nano=0)
    with pytest.raises(LedgerIntegrityError):
        initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1, opening_balance_nano=0)
    with pytest.raises(LedgerIntegrityError):
        initialize_canonical_ledger(tmp_path / "second.sqlite", s.anchor_path, lifetime_cap_nano=1, opening_balance_nano=0)
    with pytest.raises(LedgerIntegrityError):
        initialize_canonical_ledger(tmp_path / "third.sqlite", tmp_path / "other-anchor.json",
                                    lifetime_cap_nano=1, opening_balance_nano=0, other_anchor_paths=(s.anchor_path,))


def test_copied_ledger_is_refused(tmp_path):
    s = _settings()
    initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1, opening_balance_nano=0)
    copy = tmp_path / "copy.sqlite"
    shutil.copy(s.ledger_path, copy)
    with pytest.raises(LedgerIntegrityError, match="path|identity"):
        Ledger(copy, billable=True).verify_anchor(s.anchor_path)


def test_rolled_back_ledger_is_refused(tmp_path):
    s = _settings()
    led = initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1, opening_balance_nano=0)
    snapshot = tmp_path / "before.sqlite"
    shutil.copyfile(s.ledger_path, snapshot)
    led.add_adjustment(250_000_000, kind="manual", note="spend after the snapshot")
    led.raise_high_water(s.anchor_path)
    shutil.copyfile(snapshot, s.ledger_path)  # in place: same inode, older content
    with pytest.raises(LedgerIntegrityError, match="rolled back"):
        Ledger(s.ledger_path, billable=True).verify_anchor(s.anchor_path)


def test_deleted_ledger_needs_recovery_with_dashboard_figure():
    s = _settings()
    led = initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1, opening_balance_nano=0)
    led.add_adjustment(500_000_000, kind="manual")
    led.raise_high_water(s.anchor_path)
    s.ledger_path.unlink()
    fresh = Ledger(s.ledger_path, billable=True)
    with pytest.raises(LedgerIntegrityError):
        fresh.verify_anchor(s.anchor_path)
    s.ledger_path.unlink()
    with pytest.raises(LedgerIntegrityError):
        initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1, opening_balance_nano=0)
    with pytest.raises(LedgerIntegrityError, match="high-water"):
        initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1,
                                    opening_balance_nano=100_000_000, recover=True)
    recovered = initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1,
                                            opening_balance_nano=600_000_000, recover=True)
    assert recovered.verify_anchor(s.anchor_path).high_water_nano == 600_000_000


def test_anchor_on_other_machine_is_refused(monkeypatch):
    s = _settings()
    led = initialize_canonical_ledger(s.ledger_path, s.anchor_path, lifetime_cap_nano=1, opening_balance_nano=0)
    monkeypatch.setattr(ledger_mod, "machine_id", lambda: "some-other-machine")
    with pytest.raises(LedgerIntegrityError, match="different machine"):
        led.verify_anchor(s.anchor_path)


# --- live guards ----------------------------------------------------------------------------


def test_pytest_is_always_detected():
    assert running_under_pytest()


def test_live_guards_in_order(monkeypatch):
    gov = billable_governor({"EDDIE_BACKEND": "ember", "EDDIE_ALLOW_LIVE_EMBER": "true"})
    with pytest.raises(LiveEmberDisabledError, match="pytest"):
        gov.assert_live_allowed()
    monkeypatch.setattr(governor_mod, "running_under_pytest", lambda: False)
    monkeypatch.setattr(governor_mod, "running_in_container", lambda: True)
    with pytest.raises(LiveEmberDisabledError, match="container"):
        gov.assert_live_allowed()
    monkeypatch.setattr(governor_mod, "running_in_container", lambda: False)
    monkeypatch.setattr(governor_mod, "filesystem_type", lambda p: "nfs4")
    with pytest.raises(LedgerIntegrityError, match="nfs4"):
        gov.assert_live_allowed()
    monkeypatch.setattr(governor_mod, "filesystem_type", lambda p: "ext4")
    gov.assert_live_allowed()  # anchored, local fs, flags set: allowed (no network here)


def test_live_guards_require_both_flags(monkeypatch):
    monkeypatch.setattr(governor_mod, "running_under_pytest", lambda: False)
    gov = billable_governor({"EDDIE_BACKEND": "ember"})
    with pytest.raises(LiveEmberDisabledError, match="ALLOW_LIVE"):
        gov.assert_live_allowed()


def test_billable_ledger_is_never_auto_created():
    env = dict(os.environ, EDDIE_BACKEND="ember", EDDIE_ALLOW_LIVE_EMBER="true")
    settings = load_settings(env)
    with pytest.raises(LedgerIntegrityError, match="budget init"):
        SpendGovernor(settings, billable=True)
    assert not settings.ledger_path.exists()


def test_unanchored_ledger_is_refused_for_live(monkeypatch):
    monkeypatch.setattr(governor_mod, "running_under_pytest", lambda: False)
    monkeypatch.setattr(governor_mod, "running_in_container", lambda: False)
    env = dict(os.environ, EDDIE_BACKEND="ember", EDDIE_ALLOW_LIVE_EMBER="true")
    settings = load_settings(env)
    Ledger(settings.ledger_path, billable=True)  # exists, but was never anchored by `budget init`
    gov = SpendGovernor(settings, billable=True)
    with pytest.raises(LedgerIntegrityError, match="anchor"):
        gov.assert_live_allowed()


def test_filesystem_type_reads_mountinfo(tmp_path):
    assert filesystem_type(tmp_path) != "unknown"


def test_governor_settles_and_snapshots(offline_sandboxed_env):
    gov = billable_governor()
    res = gov.reserve(backend="ember", region="R1.triage", effort="minimal", max_output=64,
                      estimate=fixed_estimate(2_000), body_sha256="c" * 64, allow_unbounded=False)
    gov.dispatch(res)
    out = gov.settle_response(res, {"id": "resp_x", "status": "completed", "usage": exact_usage(1_000, 32)},
                              http_status=200, latency_ms=12)
    assert out.state == "settled" and out.tripped is None
    status = Path(gov.settings.public_dir / "status.json").read_text()
    assert '"committed_usd"' in status and "resp_x" not in status  # snapshot carries no response ids
