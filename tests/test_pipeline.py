"""run-ember end to end, offline: the rehearsal, resume, the phase budget, and every stop rule.

The mock backend has no caps, so the money rules are exercised on an ember-mode gateway whose
transport is an `httpx.MockTransport` (no socket, a temp ledger): the same governor, ledger and
wire checks a live run goes through, at $0.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest

from eddie.config import load_settings
from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone
from eddie.ingest.synthetic import write_sample_tree
from eddie.llm.estimate import estimate_body
from eddie.llm.gateway import EddieLLMGateway
from eddie.pipeline import rehearsal
from eddie.pipeline.forecast import forecast
from eddie.pipeline.orchestrator import (
    RunOptions,
    StopRun,
    audit,
    phase_spend_nano,
    run,
    run_lock,
    status_report,
)
from eddie.pipeline.select import select
from eddie.sandbox.runner import Limits, SandboxRunner
from tests.emberhelpers import ember_settings, prepare_live_state

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipeline")
    write_sample_tree(root / "tree", scale=2)
    index = root / "index.sqlite"
    build_index(root / "tree", index, workers=1, log=lambda *_: None)
    con = sqlite3.connect(index)
    names = [r[0] for r in con.execute("SELECT full_name FROM decls WHERE full_name IS NOT NULL")]
    con.close()
    spine = root / "spine.tsv.gz"
    with gzip.open(spine, "wt") as fh:
        fh.writelines(f"{n}\tthm\n" for n in names if not n.startswith("Edge."))
    cone = root / "cone.sqlite"
    build_cone(index, cone, spine_path=spine, workers=1, log=lambda *_: None)
    return index, cone, root / "mplcache"


@pytest.fixture
def world(built, tmp_path):
    index, cone, mpl = built
    data = tmp_path / "data"  # conftest points EDDIE_DATA_DIR here
    (data / "index").mkdir(parents=True)
    shutil.copy(index, data / "index" / "eddie_index.sqlite")
    shutil.copy(cone, data / "index" / "eddie_cone.sqlite")
    runner = SandboxRunner(data / "artifacts", limits=Limits(wall_s=60, cpu_s=60), tmp_root=tmp_path / "jobs", mpl_cache=mpl)
    return data, runner


def quiet(*_):
    return None


def rows(db: Path, sql: str, *args) -> list[tuple]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in con.execute(sql, args)]
    finally:
        con.close()


# --- an ember-mode gateway on a mock transport -----------------------------------------------------------


class FakeEmber:
    """Answers like Ember (rehearsal text, sane usage) unless a rule says otherwise."""

    def __init__(self, rule=None):
        self.rule = rule
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.rule is not None:
            forced = self.rule(body, len(self.bodies))
            if forced is not None:
                return forced
        return httpx.Response(200, json=ok_payload(body, rehearsal.respond(body)))


def ok_payload(body: dict, text: str, status: str = "completed") -> dict:
    est = estimate_body(body).input_tokens
    out = min(body["max_output_tokens"], 40 + len(text.encode()) // 4)
    return {"id": f"resp_{uuid.uuid4().hex[:20]}", "object": "response", "model": body["model"], "status": status,
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": max(1, est // 3), "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": out, "output_tokens_details": {"reasoning_tokens": out // 3},
                      "total_tokens": max(1, est // 3) + out}}


def live(fake: FakeEmber, **env):
    settings = ember_settings({k: str(v) for k, v in env.items()})
    prepare_live_state(settings)

    def factory(s):
        return EddieLLMGateway(s, ember_transport=httpx.MockTransport(fake))

    return settings, factory


# --- the $0 rehearsal --------------------------------------------------------------------------------------


def test_calibrate_rehearsal_writes_chapter_20_and_resumes_with_no_new_calls(world):
    data, runner = world
    settings = load_settings()
    summary = run(settings, RunOptions(phase="calibrate"), sandbox=runner, log=quiet)
    assert summary.stopped is None, summary
    db = data / "state" / "rehearsal.sqlite"
    assert db.exists() and not (data / "state" / "eddie_artifacts.sqlite").exists()  # never the canonical DB
    sel = select(data / "index" / "eddie_cone.sqlite", data / "index" / "eddie_index.sqlite")
    main = sel.pillar(sel.main_slug)
    chapters = rows(db, "SELECT pillar_slug, source, synthesis_md FROM chapters")
    assert [(c[0], c[1]) for c in chapters] == [(main.slug, "mock")]
    assert all(s in chapters[0][2] for s in ("### What this chapter does", "### How it feeds Main"))
    assert {r[0] for r in rows(db, "SELECT source FROM triage")} == {"mock"}
    assert len(rows(db, "SELECT 1 FROM triage")) == len(main.triage)
    # the calibration pair: a draft R2 rejected, then a repair it verified, linked as before and after
    attempts = rows(db, "SELECT attempt, origin, outcome FROM witness_attempts WHERE decl_full_name = ? ORDER BY attempt",
                    sel.main_theorem.full_name)
    assert attempts[:2] == [(0, "calibration_draft", "rejected"), (1, "r1", "verified")]
    (bad, good) = rows(db, "SELECT id, status, supersedes_id FROM witnesses WHERE decl_full_name = ? ORDER BY id",
                       sel.main_theorem.full_name)
    assert bad[1] == "rejected" and good[1] == "verified" and good[2] == bad[0]
    assert len(rows(db, "SELECT 1 FROM intuitions WHERE source = 'mock'")) == 2  # Main's theorem and the gate
    assert {r[0] for r in rows(db, "SELECT region FROM reviews")} == {"R2.critic", "R3.arbiter"}
    again = run(settings, RunOptions(phase="calibrate"), sandbox=runner, log=quiet)
    assert again.stopped is None and again.calls == 0  # everything was done: nothing is called twice
    report = status_report(settings)
    assert report["phases"]["calibrate"]["steps"]["consolidate"] == {"done": 1}


def test_a_crashed_run_resumes_without_paying_twice_for_any_call(world):
    data, runner = world
    fake = FakeEmber()
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    first = run(settings, RunOptions(phase="calibrate", max_calls=4), sandbox=runner, gateway_factory=factory, log=quiet)
    assert first.stopped and not first.hard and first.calls == 4
    second = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert second.stopped is None, second
    ledger = settings.ledger_path
    tags = rows(ledger, "SELECT tag FROM calls WHERE state = 'settled' AND tag LIKE 'd3:%'")
    assert len(tags) == len(set(tags)) == len(fake.bodies)  # every tag paid exactly once
    assert audit(settings) == []  # every Ember row points at a settled, tagged ledger call
    db = data / "state" / "eddie_artifacts.sqlite"
    assert rows(db, "SELECT COUNT(*) FROM chapters WHERE source = 'ember' AND response_id IS NOT NULL")[0][0] == 1
    trace = json.loads((Path(settings.public_dir) / "telemetry.json").read_text())
    assert trace["totals"]["calls"] == len(fake.bodies)


def test_the_phase_budget_holds_across_processes(world):
    data, runner = world
    fake = FakeEmber()
    settings, factory = live(fake, EDDIE_DATA_DIR=data, EDDIE_RUN_CAP_USD="0.003")
    first = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert first.stopped and not first.hard and "run cap" in first.stopped
    spent = phase_spend_nano(settings.ledger_path, "calibrate")
    assert 0 < spent <= settings.run_cap_nano
    second = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert phase_spend_nano(settings.ledger_path, "calibrate") <= settings.run_cap_nano  # never a fresh cap
    assert second.stopped


def test_a_refused_json_schema_switches_to_prompt_json_instead_of_aborting(world):
    data, runner = world

    def no_schemas(body, n):
        if "text" in body:
            return httpx.Response(400, json={"error": {"message": "text.format is not supported"}})
        return None

    fake = FakeEmber(no_schemas)
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    summary = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert summary.stopped is None, summary
    assert any("prompt-JSON" in n for n in summary.notes)
    assert sum("text" in b for b in fake.bodies) == 1  # exactly one structured request was ever sent
    db = data / "state" / "eddie_artifacts.sqlite"
    assert rows(db, "SELECT value FROM meta WHERE key = 'pipeline.structured'") == [("0",)]
    assert rows(db, "SELECT COUNT(*) FROM chapters WHERE source = 'ember'")[0][0] == 1
    breaker = rows(settings.ledger_path, "SELECT state FROM breaker")[0][0]
    assert breaker == "closed"


def test_any_other_4xx_aborts_the_phase(world):
    data, runner = world
    fake = FakeEmber(lambda body, n: httpx.Response(422, json={"error": {"message": "bad"}}) if n == 1 else None)
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    summary = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert summary.stopped and summary.hard and "HTTP 422" in summary.stopped
    assert len(fake.bodies) == 1


def test_truncated_replies_count_as_strikes_and_stop_the_run(world):
    data, runner = world

    def truncate_triage(body, n):
        if body["max_output_tokens"] > 128 and "R1.triage" in json.dumps(body.get("prompt_cache_key", "")):
            return httpx.Response(200, json=ok_payload(body, "", status="incomplete"))
        return None

    fake = FakeEmber(truncate_triage)
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    summary = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert summary.stopped and summary.hard and "3 failed calls in a row" in summary.stopped
    assert rows(settings.ledger_path, "SELECT state FROM breaker")[0][0] == "closed"  # we stopped first


def test_only_one_run_at_a_time(world, tmp_path):
    lock = tmp_path / "state" / "run-ember.lock"
    with run_lock(lock), pytest.raises(StopRun):
        with run_lock(lock):
            pass


def test_full_atlas_waits_for_calibrate(world):
    _, runner = world
    with pytest.raises(StopRun, match="calibrate"):
        run(load_settings(), RunOptions(phase="full-atlas"), sandbox=runner, log=quiet)


def test_the_forecast_prices_every_planned_call_without_calling(world):
    data, _ = world
    settings = load_settings()
    cal = forecast(settings, RunOptions(phase="calibrate"))
    assert {"R1.triage", "R1.worker", "R2.critic", "R3.arbiter", "R4.consolidator"} <= set(cal.lines)
    assert 0 < cal.expected_nano < cal.worst_nano
    full = forecast(settings, RunOptions(phase="full-atlas"))
    assert full.lines["R4.consolidator"].calls >= 2 and full.worst_nano > 0
    assert not (data / "state").exists() or not any((data / "state").iterdir())  # a dry run writes nothing


def test_the_in_process_sandbox_refuses_a_parents_environment(world):
    _, runner = world
    res = asyncio.run(runner.run("import os\nprint(open(f'/proc/{os.getppid()}/environ', 'rb').read()[:8])\n"))
    assert res.status == "policy_violation"
    assert any(v["event"] == "read" and "/environ" in v["detail"] for v in res.violations)


def test_an_unusable_chapter_reply_is_retried_once_at_medium(world):
    data, runner = world
    seen = {"r4": 0}

    def garble_first_chapter(body, n):
        if body["max_output_tokens"] > 128 and "R4.consolidator" in body.get("prompt_cache_key", ""):
            seen["r4"] += 1
            if seen["r4"] == 1:
                return httpx.Response(200, json=ok_payload(body, "not json at all"))
        return None

    fake = FakeEmber(garble_first_chapter)
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    summary = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert summary.stopped is None, summary
    r4 = [b for b in fake.bodies if b["max_output_tokens"] > 128 and "R4.consolidator" in b.get("prompt_cache_key", "")]
    assert [b["reasoning"]["effort"] for b in r4] == ["high", "medium"]
    db = data / "state" / "eddie_artifacts.sqlite"
    assert rows(db, "SELECT COUNT(*) FROM chapters WHERE source = 'ember'")[0][0] == 1
