"""`run-ember --dry-run`: the exact worst case of every call the phase would still make.

Requests whose inputs are known now (probes, triage batches, the gate audits, the chapters'
structure) are built exactly as the run would build them and sized with the governor's own
estimator. Inputs that only exist later (a gloss, a script, a critique) are taken at their caps,
which is what keeps the worst case a hard bound. The expected cost uses what the ledger measured on
earlier pipeline calls of the same region: the input ratio, and the mean output in tokens (capped
at the call's ceiling; a reasoning reply does not grow when its ceiling is raised). Before
calibrate it uses stated ratios.
Nothing here constructs a gateway or writes any ledger.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eddie.config import HARD_INPUT_TOKEN_LIMIT, NANO_PER_USD, Settings
from eddie.llm import pricing
from eddie.llm.body import build_body
from eddie.llm.errors import HardInputTokenLimitError
from eddie.llm.estimate import estimate_body, image_tokens_upper
from eddie.llm.policy import check_request, validate_wire_body
from eddie.llm.types import EddieRequest
from eddie.pipeline import prompts
from eddie.pipeline.calls import TAG_PREFIX, build_request, text_part
from eddie.pipeline.context import (
    CARRY_CODE,
    CARRY_CRITIQUE,
    CARRY_HISTORY,
    CARRY_INTUITION,
    card,
    neighbours,
    render,
)
from eddie.pipeline.orchestrator import (
    ARBITER_MAX,
    PROBE_MAX,
    R4_MAX,
    TRIAGE_MAX,
    RunOptions,
    RunPaths,
    _facts_text,
    _triage_prompt,
    batch_key,
    flagship_ref,
    phase_spend_nano,
    unverified_flagships,
)
from eddie.pipeline.select import open_cone, select
from eddie.pipeline.witness import CRITIC_MAX, WORKER_MAX_HIGH, schedule
from eddie.planner.chapters import chapter_facts

# Before any pipeline call has settled: typical input as a share of the byte bound, and output
# (reasoning included) as a share of max_output_tokens. Calibrate replaces both with measurements.
DEFAULT_IN = {"R1.triage": 0.33, "R1.worker": 0.35, "R2.critic": 0.25, "R3.arbiter": 0.33, "R4.consolidator": 0.33}
DEFAULT_OUT = {"R1.triage": 0.63, "R1.worker": 0.41, "R2.critic": 0.59, "R3.arbiter": 0.61, "R4.consolidator": 0.40}
EXPECTED_REPAIRS = 0.7
# the repair phase: how likely each of its high-effort attempts is to be needed, and its chapter rewrite
REPAIR_WEIGHTS = (1.0, 0.6, 0.4)
REPAIR_REWRITE_WEIGHT = 0.7
PNG_BOUND = image_tokens_upper(1280, 760)
FILL_WORDS = " ".join(["word"] * 60)


def bound_tokens(req: EddieRequest) -> int:
    """The governor's pre-flight, without a gateway: policy, the wire schema, the upper bound."""
    check_request(req)
    body = build_body(req)
    validate_wire_body(body)
    est = estimate_body(body)
    if est.input_tokens > HARD_INPUT_TOKEN_LIMIT:
        raise HardInputTokenLimitError(est.input_tokens, HARD_INPUT_TOKEN_LIMIT)
    return est.input_tokens


@dataclass
class Line:
    region: str
    calls: int = 0
    worst_nano: int = 0
    expected_nano: int = 0


@dataclass
class Forecast:
    phase: str
    lines: dict[str, Line] = field(default_factory=dict)
    run_cap_nano: int = 0
    phase_spent_nano: int = 0
    daily_headroom_nano: int | None = None
    lifetime_headroom_nano: int | None = None
    measured: dict[str, tuple[float, float]] = field(default_factory=dict)  # region -> (input ratio, mean output tokens)
    skipped_done: int = 0

    def add(self, region: str, bound: int, max_out: int, *, weight: float = 1.0, worst: bool = True) -> None:
        line = self.lines.setdefault(region, Line(region))
        if region in self.measured:
            r_in, mean_out = self.measured[region]
            out = min(max_out, round(mean_out))
        else:
            r_in, out = DEFAULT_IN.get(region, 0.33), round(max_out * DEFAULT_OUT.get(region, 0.5))
        if worst:
            line.calls += 1
            line.worst_nano += pricing.reserve_nano(bound, max_out)
        line.expected_nano += int(weight * pricing.cost_nano(max(1, round(bound * r_in)), 0, out))

    @property
    def worst_nano(self) -> int:
        return sum(x.worst_nano for x in self.lines.values())

    @property
    def expected_nano(self) -> int:
        return sum(x.expected_nano for x in self.lines.values())

    @property
    def remaining_nano(self) -> int:
        return max(0, self.run_cap_nano - self.phase_spent_nano)

    def verdict(self) -> str:
        if self.worst_nano > self.remaining_nano:
            return (f"the worst case ${self.worst_nano / NANO_PER_USD:.4f} exceeds this phase's remaining budget "
                    f"${self.remaining_nano / NANO_PER_USD:.4f}: the run will stop at the cap and resume next time")
        return "fits: the worst case is inside this phase's remaining budget"


def measured_ratios(ledger: Path) -> dict[str, tuple[float, float]]:
    if not ledger.exists():
        return {}
    con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT region, AVG(1.0 * input_tokens / est_input), AVG(1.0 * output_tokens), COUNT(*) "
            "FROM calls WHERE state = 'settled' AND tag LIKE ? AND est_input > 0 AND max_output > 128 GROUP BY region",
            (f"{TAG_PREFIX}:%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()
    return {r[0]: (float(r[1]), float(r[2])) for r in rows if r[3] >= 1}


def done_steps(db: Path) -> set[str]:
    """Finished step keys, read without opening the store as a writer (a dry run writes nothing)."""
    if not db.exists():
        return set()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT step_key FROM pipeline_steps WHERE status = 'done'")}
    except sqlite3.OperationalError:  # a schema-1 database has no step queue yet
        return set()
    finally:
        con.close()


def headroom(settings: Settings) -> tuple[int | None, int | None]:
    """(daily, lifetime) headroom from the canonical ledger, read-only."""
    ledger = settings.ledger_path
    if not ledger.exists():
        return None, None
    import time

    con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        committed = con.execute("SELECT COALESCE(SUM(actual_nano), 0) FROM calls WHERE billable = 1 AND state IN "
                                "('settled', 'orphan_charged')").fetchone()[0] + con.execute(
                                "SELECT COALESCE(SUM(nano), 0) FROM adjustments").fetchone()[0]
        since = time.time() - 86_400
        daily = con.execute("SELECT COALESCE(SUM(actual_nano), 0) FROM calls WHERE billable = 1 AND state IN "
                            "('settled', 'orphan_charged') AND finished_at >= ?", (since,)).fetchone()[0]
        persisted = con.execute("SELECT value FROM meta WHERE key = 'lifetime_cap_nano'").fetchone()
    finally:
        con.close()
    lifetime = min(settings.lifetime_cap_nano, int(persisted[0])) if persisted else settings.lifetime_cap_nano
    return settings.daily_cap_nano - int(daily), lifetime - int(committed)


def forecast(settings: Settings, opts: RunOptions) -> Forecast:
    # a forecast is always of the live run: the canonical database's steps count, never a rehearsal's
    paths_ = RunPaths.for_settings(settings, live=True, db=opts.db)
    fc = Forecast(opts.phase, run_cap_nano=settings.run_cap_nano)
    fc.measured = measured_ratios(settings.ledger_path)
    fc.phase_spent_nano = phase_spend_nano(settings.ledger_path, opts.phase)  # money is spent only there
    fc.daily_headroom_nano, fc.lifetime_headroom_nano = headroom(settings)
    sel = select(paths_.cone, paths_.index, plan_path=paths_.plan)
    done = done_steps(paths_.db)
    structured = opts.structured if opts.structured is not None else True
    phase = opts.phase

    def req(region: str, effort: str, max_out: int, instructions: str, text: str, schema: str, *, image: bool = False,
            summary: str | None = None) -> int:
        parts = [text_part(text)]
        r = build_request(phase=phase, structured=structured, step_key="forecast", attempt=0, region=region, effort=effort,
                          max_output=max_out, instructions=instructions, parts=parts, schema=schema, summary=summary)
        return bound_tokens(r) + (PNG_BOUND if image else 0)

    if phase == "repair":
        con = open_cone(paths_.cone, paths_.index)
        try:
            repair_lines(fc, req, con, sel, done, paths_.db, opts)
        finally:
            con.close()
        return fc
    calibrate = phase == "calibrate"
    plans = [p for p in sel.pillars if (p.slug == sel.main_slug) == calibrate]
    if opts.only:
        plans = [p for p in plans if p.slug == opts.only]
    con = open_cone(paths_.cone, paths_.index)
    try:
        facts = {f["slug"]: f for f in chapter_facts(con)}
        if calibrate and not any(k.startswith("probe:") for k in done):
            for region, effort, instr, schema, summary, image in (
                ("R1.triage", opts.triage_effort, prompts.TRIAGE, "triage", None, False),
                ("R1.worker", "medium", prompts.WORKER, "worker", None, False),
                ("R2.critic", "medium", prompts.CRITIC, "critic", None, True),
                ("R3.arbiter", opts.arbiter_effort, prompts.ARBITER, "arbiter", "concise", False),
                ("R4.consolidator", "high", prompts.CONSOLIDATOR, "chapter", None, False),
            ):
                b = req(region, effort, PROBE_MAX, instr, "probe", schema, image=image, summary=summary)
                fc.add(region, b, PROBE_MAX)
        for plan in plans:
            todo = list(plan.triage)
            size = max(1, opts.batch_size)
            for i in range(0, len(todo), size):
                batch = todo[i : i + size]
                if batch_key(plan.slug, batch) in done:
                    fc.skipped_done += 1
                    continue
                text, _ = _triage_prompt(con, batch)
                fc.add("R1.triage", req("R1.triage", opts.triage_effort, TRIAGE_MAX, prompts.TRIAGE, text, "triage"), TRIAGE_MAX)
            targets = []
            if calibrate:
                targets = [d for d in (sel.main_theorem, plan.gate) if d is not None]
            elif plan.gate is not None:
                targets = [plan.gate] + ([plan.chokepoints[1]] if len(plan.chokepoints) > 1 else [])
            for n_target, t in enumerate({d.id: d for d in targets}.values()):
                if f"witness:{t.full_name}" in done:
                    continue
                extra = not calibrate and n_target > 0  # an extra witness happens for at most 3 chapters
                tc, nbs = card(con, t.id), neighbours(con, t.id)
                base = "\n".join([render(tc, label="target"), f"gloss: {FILL_WORDS}", *(render(n, with_context=False) for n in nbs)])
                repair = base + "\n" + "x" * (CARRY_CODE + CARRY_CRITIQUE)
                draft = calibrate and opts.calibration_pair and sel.main_theorem is not None and t.id == sel.main_theorem.id
                for n, (origin, effort, max_out) in enumerate(schedule(draft)):
                    text = repair if n > 0 and origin == "r1" else base
                    first = n == 0 or (draft and n == 1)
                    weight = (1.0 if first else EXPECTED_REPAIRS / 3) * (0.3 if extra else 1.0)
                    instr = prompts.WORKER_DRAFT if origin == "calibration_draft" else prompts.WORKER
                    fc.add("R1.worker", req("R1.worker", effort, max_out, instr, text, "worker"), max_out, weight=weight)
                    crit = "\n".join([render(tc, with_context=False), f"caption: {FILL_WORDS}", "qc {}"])
                    fc.add("R2.critic", req("R2.critic", "medium", CRITIC_MAX, prompts.CRITIC, crit, "critic", image=True),
                           CRITIC_MAX, weight=weight)
            gates = [plan.gate] if plan.gate else []
            if calibrate and sel.main_theorem is not None:
                gates = [sel.main_theorem, *gates]
            for g in {d.id: d for d in gates}.values():
                if f"arbiter:{g.full_name}" in done:
                    continue
                tc, nbs = card(con, g.id), neighbours(con, g.id)
                text = "\n".join([render(tc, label="target"), "x" * 3_000, *(render(n, with_context=False) for n in nbs),
                                  f"gloss: {FILL_WORDS}", f"witness: {FILL_WORDS}"])
                for attempt in range(2):  # one retry is allowed
                    fc.add("R3.arbiter", req("R3.arbiter", opts.arbiter_effort, ARBITER_MAX, prompts.ARBITER, text, "arbiter",
                                             summary="concise"), ARBITER_MAX, weight=1.0 if attempt == 0 else 0.0)
            if f"r4:{plan.slug}" not in done and plan.slug in facts:
                chapter_lines(fc, req, facts[plan.slug], plan, weight=1.0)
    finally:
        con.close()
    return fc


def chapter_lines(fc: Forecast, req: Callable[..., int], fact: dict[str, Any], plan: Any, *, weight: float) -> None:
    """One R4 chapter at its caps: the high-effort call and the medium retry."""
    text = "\n".join([_facts_text(fact), *(f"- `{d.full_name}`: {FILL_WORDS}" for d in plan.triage[:40]),
                      "x" * CARRY_INTUITION, *(f"witness: {FILL_WORDS}" for _ in range(3)),
                      "Allowed: " + ", ".join(d.full_name for d in plan.triage)])
    for attempt in range(2):
        fc.add("R4.consolidator", req("R4.consolidator", "high" if attempt == 0 else "medium", R4_MAX,
                                      prompts.CONSOLIDATOR, text, "chapter"), R4_MAX,
               weight=weight * (1.0 if attempt == 0 else 0.15))


def repair_lines(fc: Forecast, req: Callable[..., int], con: sqlite3.Connection, sel: Any, done: set[str], db: Path,
                 opts: RunOptions) -> None:
    """The repair phase: every flagship still without a verified witness, three high-effort attempts each at
    their caps, and one R4 rewrite per chapter (the worst case assumes every flagship verifies)."""
    todo: list[tuple[str, str]] = []
    if db.exists():
        art = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            todo = unverified_flagships(art, "ember")
        except sqlite3.OperationalError:
            todo = []
        finally:
            art.close()
    if opts.only:
        todo = [t for t in todo if t[0] == opts.only]
    plans = {p.slug: p for p in sel.pillars}
    facts = {f["slug"]: f for f in chapter_facts(con)}
    chapters: set[str] = set()
    for slug, decl in todo:
        plan = plans.get(slug)
        t = flagship_ref(sel, plan, decl) if plan else None
        if t is None or f"repair:{decl}" in done:
            continue
        tc, nbs = card(con, t.id), neighbours(con, t.id)
        base = "\n".join([render(tc, label="target"), f"gloss: {FILL_WORDS}", *(render(n, with_context=False) for n in nbs)])
        text = "\n".join([base, prompts.REPAIR_PASS, "x" * (CARRY_CODE + CARRY_HISTORY)])
        crit = "\n".join([render(tc, with_context=False), f"caption: {FILL_WORDS}", "qc {}"])
        for weight in REPAIR_WEIGHTS:
            fc.add("R1.worker", req("R1.worker", "high", WORKER_MAX_HIGH, prompts.WORKER, text, "worker"), WORKER_MAX_HIGH,
                   weight=weight)
            fc.add("R2.critic", req("R2.critic", "medium", CRITIC_MAX, prompts.CRITIC, crit, "critic", image=True),
                   CRITIC_MAX, weight=weight)
        if slug not in chapters and f"r4:{slug}:repair" not in done and slug in facts:
            chapters.add(slug)
            chapter_lines(fc, req, facts[slug], plan, weight=REPAIR_REWRITE_WEIGHT)


def render_forecast(fc: Forecast) -> list[str]:
    usd = lambda n: f"${n / NANO_PER_USD:.4f}"  # noqa: E731
    out = [f"dry run: {fc.phase} (no model is called)"]
    for line in fc.lines.values():
        measured = " (measured)" if line.region in fc.measured else ""
        out.append(f"  {line.region:<16} {line.calls:>4} calls max   worst {usd(line.worst_nano)}   expected "
                   f"{usd(line.expected_nano)}{measured}")
    out.append(f"  total            worst {usd(fc.worst_nano)} (hard bound)   expected {usd(fc.expected_nano)}")
    out.append(f"  phase budget     {usd(fc.run_cap_nano)}, already spent {usd(fc.phase_spent_nano)}, remaining "
               f"{usd(fc.remaining_nano)}")
    if fc.daily_headroom_nano is not None:
        out.append(f"  ledger headroom  rolling 24 h {usd(fc.daily_headroom_nano)}, lifetime {usd(fc.lifetime_headroom_nano or 0)}")
    if fc.skipped_done:
        out.append(f"  ({fc.skipped_done} triage batches already done are not counted)")
    out.append(f"  {fc.verdict()}")
    return out


def as_json(fc: Forecast) -> dict[str, Any]:
    return {"phase": fc.phase, "worst_usd": fc.worst_nano / NANO_PER_USD, "expected_usd": fc.expected_nano / NANO_PER_USD,
            "regions": {k: {"calls": v.calls, "worst_usd": v.worst_nano / NANO_PER_USD,
                            "expected_usd": v.expected_nano / NANO_PER_USD} for k, v in fc.lines.items()},
            "remaining_usd": fc.remaining_nano / NANO_PER_USD, "measured": fc.measured}
