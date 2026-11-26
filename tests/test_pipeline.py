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
from eddie.pipeline.forecast import Forecast, forecast
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
    report = status_report(settings, db=db)  # a rehearsal is inspected with --db; status defaults to the live run
    assert report["kind"] == "other" and report["phases"]["calibrate"]["steps"]["consolidate"] == {"done": 1}


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


# --- found on the first live calibrate run ----------------------------------------------------------------


def test_status_shows_the_live_database_and_real_spend_from_a_plain_shell(world, monkeypatch):
    from eddie.llm.policy import REGION_POLICY
    from eddie.pipeline import orchestrator
    from eddie.pipeline.witness import CRITIC_MAX
    from eddie.state.artifacts import ArtifactStore

    data, _ = world
    settings = load_settings()  # what a plain `uv run` gets: the mock backend
    assert settings.backend != "ember"
    (data / "state").mkdir(parents=True, exist_ok=True)
    live, rehearsal = data / "state" / "eddie_artifacts.sqlite", data / "state" / "rehearsal.sqlite"
    ArtifactStore(live).ensure_step(phase="calibrate", step_key="r4:main", tag="d3:calibrate:x:a0", kind="consolidate",
                                    source="ember")
    ArtifactStore(rehearsal)
    seen = []
    monkeypatch.setattr(orchestrator, "phase_spend_nano", lambda ledger, phase: seen.append(ledger) or 20_583_000)
    report = orchestrator.status_report(settings)
    assert Path(report["db"]) == live and report["kind"] == "live"
    assert report["phases"]["calibrate"]["steps"] and report["phases"]["calibrate"]["spent_usd"] == pytest.approx(0.020583)
    assert set(seen) == {settings.ledger_path}  # never the shadow ledger a mock rehearsal writes
    assert orchestrator.status_report(settings, db=rehearsal)["kind"] == "other"
    assert 2_048 < CRITIC_MAX <= REGION_POLICY["R2.critic"].max_output_ceiling


def test_the_forecast_expects_the_measured_output_whatever_the_ceiling():
    measured = {"R2.critic": (0.5, 1_600.0)}
    expected = []
    for cap in (2_048, 4_096, 8_192):
        fc = Forecast("full-atlas", measured=measured)
        fc.add("R2.critic", 2_000, cap)
        expected.append(fc.expected_nano)
    assert expected[0] == expected[1] == expected[2]  # a reasoning reply does not grow with its ceiling
    capped = Forecast("full-atlas", measured=measured)
    capped.add("R2.critic", 2_000, 1_024)
    assert capped.expected_nano < expected[0]  # but it never exceeds the ceiling of the call
    before = [Forecast("calibrate") for _ in range(2)]  # before calibrate: the stated share of the cap
    before[0].add("R2.critic", 2_000, 2_048)
    before[1].add("R2.critic", 2_000, 4_096)
    assert before[1].expected_nano > before[0].expected_nano


@pytest.mark.parametrize("second_opinion", ["verified", "rejected"])
def test_a_render_whose_review_was_cut_off_is_reviewed_again_on_the_next_run(world, second_opinion):
    data, runner = world
    sel = select(data / "index" / "eddie_cone.sqlite", data / "index" / "eddie_index.sqlite")
    gate = sel.pillar(sel.main_slug).gate
    assert gate is not None and gate.id != sel.main_theorem.id
    state = {"run": 1, "answered": False}

    def gate_review(body) -> bool:
        text = json.dumps(body.get("input", ""))
        return body["max_output_tokens"] > 128 and "input_image" in text and gate.full_name in text

    def rule(body, n):
        if not gate_review(body):
            return None
        if state["run"] == 1:  # every review of the gate's render is cut off before its verdict
            return httpx.Response(200, json=ok_payload(body, "", status="incomplete"))
        if not state["answered"]:  # the next run's second opinion on the same render
            state["answered"] = True
            rejected = second_opinion == "rejected"
            return httpx.Response(200, json=ok_payload(body, json.dumps({
                "verdict": second_opinion, "legibility": 2, "faithfulness": 2,
                "defects": ["the title is clipped"] if rejected else [], "fixes": ["shorten the title"] if rejected else [],
                "summary": f"second opinion: {second_opinion}"})))
        return None

    fake = FakeEmber(rule)
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    first = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert first.stopped is None, first  # two cut-off reviews in a row are strikes, not an abort
    db = data / "state" / "eddie_artifacts.sqlite"
    attempts = "SELECT attempt, outcome FROM witness_attempts WHERE decl_full_name = ? ORDER BY attempt"
    renders = "SELECT id, status, supersedes_id FROM witnesses WHERE decl_full_name = ? ORDER BY id"
    assert rows(db, attempts, gate.full_name) == [(0, "critic_unusable")]
    assert [r[1] for r in rows(db, renders, gate.full_name)] == ["pending"]

    state["run"] = 2
    before = len(fake.bodies)
    second = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert second.stopped is None, second
    sent = fake.bodies[before:]
    witnesses = rows(db, renders, gate.full_name)
    if second_opinion == "verified":
        assert rows(db, attempts, gate.full_name) == [(0, "verified")]
        assert [w[1] for w in witnesses] == ["verified"] and len(sent) == 1  # one review; nothing redrawn
    else:
        assert rows(db, attempts, gate.full_name) == [(0, "rejected"), (1, "verified")]
        assert [w[1] for w in witnesses] == ["rejected", "verified"] and witnesses[1][2] == witnesses[0][0]
        assert len(sent) == 3  # the review, one repaired script, its review
        assert "shorten the title" in json.dumps(sent[1].get("input", ""))  # R2's critique reached the repair
    second_review = rows(db, "SELECT response_id FROM reviews WHERE region = 'R2.critic' AND witness_id = ?", witnesses[0][0])
    assert second_review and second_review[0][0]  # an audited review of the same render
    assert audit(settings) == []
    third = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert third.stopped is None and third.calls == 0  # nothing is pending any more: nothing is called


def test_a_retry_after_a_cut_off_review_is_a_fresh_call(world):
    data, runner = world
    sel = select(data / "index" / "eddie_cone.sqlite", data / "index" / "eddie_index.sqlite")
    gate = sel.pillar(sel.main_slug).gate
    reviews: list[int] = []

    def rule(body, n):
        text = json.dumps(body.get("input", ""))
        if body["max_output_tokens"] > 128 and "input_image" in text and gate.full_name in text:
            reviews.append(n)
            if len(reviews) == 1:  # only the first review of the gate's render is cut off
                return httpx.Response(200, json=ok_payload(body, "", status="incomplete"))
        return None

    fake = FakeEmber(rule)
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    summary = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert summary.stopped is None, summary
    assert len(reviews) == 2  # the retry went to the model; it did not read the cut-off answer back
    db = data / "state" / "eddie_artifacts.sqlite"
    assert rows(db, "SELECT attempt, outcome FROM witness_attempts WHERE decl_full_name = ? ORDER BY attempt",
                gate.full_name) == [(0, "verified")]
    tags = rows(settings.ledger_path, "SELECT tag FROM calls WHERE state = 'settled' AND tag LIKE 'd3:%'")
    assert len(tags) == len(set(tags)) == len(fake.bodies)  # two calls, two tags, each paid once


def test_the_repair_phase_retries_unverified_flagships_and_rewrites_their_chapter(world):
    data, runner = world
    sel = select(data / "index" / "eddie_cone.sqlite", data / "index" / "eddie_index.sqlite")
    main = sel.pillar(sel.main_slug)
    gate = main.gate
    assert gate is not None and gate.id != sel.main_theorem.id
    state = {"run": 1, "reviews": 0}

    def about_gate(body) -> bool:
        return gate.full_name in json.dumps(body.get("input", "")) and body["max_output_tokens"] > 128

    def rule(body, n):
        if not (about_gate(body) and "input_image" in json.dumps(body.get("input", ""))):
            return None
        state["reviews"] += 1
        k = state["reviews"]
        if state["run"] == 1 or k == 4:  # R2 rejects all three first attempts, and the first repair attempt
            return httpx.Response(200, json=ok_payload(body, json.dumps({
                "verdict": "rejected", "legibility": 1, "faithfulness": 2, "defects": [f"defect {k}"],
                "fixes": [f"fix {k}"], "summary": f"rejected {k}"})))
        return None  # the rehearsal critic verifies

    fake = FakeEmber(rule)
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    assert run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet).stopped is None
    db = data / "state" / "eddie_artifacts.sqlite"
    attempts = "SELECT attempt, origin, effort, outcome FROM witness_attempts WHERE decl_full_name = ? ORDER BY attempt"
    assert [a[3] for a in rows(db, attempts, gate.full_name)] == ["rejected"] * 3
    main_before = rows(db, attempts, sel.main_theorem.full_name)
    chapter_before = rows(db, "SELECT response_id FROM chapters WHERE pillar_slug = ?", main.slug)[0][0]

    fc = forecast(settings, RunOptions(phase="repair"))  # $0: priced before anything is sent
    assert fc.lines["R1.worker"].calls == 3 and fc.lines["R2.critic"].calls == 3
    assert fc.lines["R4.consolidator"].calls == 2 and 0 < fc.expected_nano < fc.worst_nano

    state["run"] = 2
    before = len(fake.bodies)
    summary = run(settings, RunOptions(phase="repair"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert summary.stopped is None, summary
    sent = fake.bodies[before:]
    assert len(sent) == 5  # repair attempt, its review, repair attempt, its review, the chapter
    assert rows(db, attempts, gate.full_name)[3:] == [(3, "repair", "high", "rejected"), (4, "repair", "high", "verified")]
    workers = [json.dumps(b["input"]) for b in sent if (b.get("text") or {}).get("format", {}).get("name") == "eddie_worker"]
    assert len(workers) == 2 and all("This is a repair pass" in w for w in workers)
    assert all(f"defect {k}" in workers[0] for k in (1, 2, 3)) and "defect 4" not in workers[0]
    assert all(f"defect {k}" in workers[1] for k in (1, 2, 3, 4))  # every earlier defect, not only the last
    assert rows(db, attempts, sel.main_theorem.full_name) == main_before  # a verified flagship is left alone
    chapter_after = rows(db, "SELECT response_id FROM chapters WHERE pillar_slug = ?", main.slug)[0][0]
    assert chapter_after and chapter_after != chapter_before  # written again around the verified figure
    repair_tags = rows(settings.ledger_path, "SELECT tag FROM calls WHERE tag LIKE 'd3:repair:%' AND state = 'settled'")
    assert len(repair_tags) == len(sent) and phase_spend_nano(settings.ledger_path, "repair") > 0  # its own budget
    assert audit(settings) == []
    assert not forecast(settings, RunOptions(phase="repair")).lines  # nothing left to repair
    again = run(settings, RunOptions(phase="repair"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert again.stopped is None and again.calls == 0


def test_the_expand_phase_deepens_triage_and_draws_new_flagships_outside_the_summit(world, monkeypatch):
    from eddie.pipeline import orchestrator

    monkeypatch.setattr(orchestrator, "PER_PILLAR", 2)  # a shallow first selection leaves room to expand into
    data, runner = world
    fake = FakeEmber()
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    for phase in ("calibrate", "full-atlas"):
        assert run(settings, RunOptions(phase=phase), sandbox=runner, gateway_factory=factory, log=quiet).stopped is None
    db = data / "state" / "eddie_artifacts.sqlite"
    sel = select(data / "index" / "eddie_cone.sqlite", data / "index" / "eddie_index.sqlite")
    summit = {d.full_name for d in sel.pillar(sel.main_slug).triage} | {sel.main_theorem.full_name}
    triaged = {r[0] for r in rows(db, "SELECT decl_full_name FROM triage WHERE source = 'ember'")}
    witnessed = {r[0] for r in rows(db, "SELECT DISTINCT decl_full_name FROM witness_attempts")}
    opts = RunOptions(phase="expand", expand_triage=5, expand_witnesses=1)
    fc = forecast(settings, opts)  # $0 first
    assert fc.lines["R1.triage"].calls >= 1 and fc.lines["R1.worker"].calls >= 6  # every attempt, repairs included
    before = len(fake.bodies)
    summary = run(settings, opts, sandbox=runner, gateway_factory=factory, log=quiet)
    assert summary.stopped is None, summary
    new_triage = {r[0] for r in rows(db, "SELECT decl_full_name FROM triage WHERE source = 'ember'")} - triaged
    new_witnessed = {r[0] for r in rows(db, "SELECT DISTINCT decl_full_name FROM witness_attempts")} - witnessed
    assert new_triage and new_witnessed
    assert not (new_triage | new_witnessed) & summit  # the summit drawer is left exactly as it was
    for decl in new_witnessed:
        assert ("verified",) in rows(db, "SELECT outcome FROM witness_attempts WHERE decl_full_name = ?", decl)
    expand_tags = rows(settings.ledger_path, "SELECT tag FROM calls WHERE tag LIKE 'd3:expand:%' AND state = 'settled'")
    assert len(expand_tags) == len(fake.bodies) - before  # its own budget: every call carries the phase's tag
    assert audit(settings) == []
    again = run(settings, opts, sandbox=runner, gateway_factory=factory, log=quiet)
    assert again.stopped is None and again.calls == 0  # the picks are recorded: a rerun chooses no more


def test_a_time_cap_stops_before_the_next_call_and_the_run_resumes(world):
    data, runner = world
    fake = FakeEmber()
    settings, factory = live(fake, EDDIE_DATA_DIR=data)
    capped = run(settings, RunOptions(phase="calibrate", max_minutes=1e-9), sandbox=runner, gateway_factory=factory,
                 log=quiet)
    assert capped.stopped and "--max-minutes" in capped.stopped and not capped.hard
    assert capped.calls == 0 and not fake.bodies  # nothing was sent after the deadline
    resumed = run(settings, RunOptions(phase="calibrate"), sandbox=runner, gateway_factory=factory, log=quiet)
    assert resumed.stopped is None, resumed

