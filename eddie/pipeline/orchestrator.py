"""`run-ember`: phases, the resumable step queue, the phase budget, and the stop rules.

Money rules this file keeps (see the plan, section 1.5):

* Every paid call goes through a *call slot*: a `pipeline_steps` row of kind `call` whose
  `attempts` counter is persisted before the call, and whose ledger tag names the slot and the
  attempt. A resumed run first asks the ledger about the last attempt's tag: a settled call
  whose payload was journaled is reused, never paid again.
* The run cap is a *phase* budget: `--run-cap-usd` minus what this phase's tags already cost,
  so resuming never starts a fresh cap.
* Nothing in flight is ever cancelled. A stop (a cap, a breaker, Ctrl-C) stops scheduling and
  lets in-flight calls settle. Only a second Ctrl-C cancels, and the gateway then charges the
  reservation instead of leaving it dispatched.
* All artifact-database writes go through one writer thread, and a step's outputs commit in
  the same transaction as its status.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import signal
import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from eddie import paths
from eddie.config import NANO_PER_USD, Settings
from eddie.llm.adapters.base import output_text
from eddie.llm.errors import (
    BudgetExceededError,
    CircuitOpenError,
    GuardError,
    KillSwitchError,
    LedgerIntegrityError,
    LiveEmberDisabledError,
    TransportGuardError,
    UpstreamError,
)
from eddie.llm.gateway import EddieLLMGateway
from eddie.llm.types import EddieResponse, Usage
from eddie.pipeline import prompts
from eddie.pipeline.calls import (
    Caller,
    CallResult,
    Journal,
    interpret,
    make_tag,
    phase_tag_like,
    text_part,
)
from eddie.pipeline.context import (
    CARRY_INTUITION,
    body_excerpt,
    card,
    clip,
    neighbours,
    render,
)
from eddie.pipeline.schemas import chapter_problems, prose
from eddie.pipeline.select import DeclRef, PillarPlan, Selection, open_cone, select
from eddie.pipeline.witness import WitnessJob, run_witness
from eddie.planner.chapters import chapter_facts, short_title
from eddie.state.artifacts import ArtifactStore

PHASES = ("calibrate", "full-atlas", "repair")
REPAIR_ATTEMPTS = 3  # high-effort attempts the repair phase adds for a flagship with no verified witness
TRIAGE_MAX = 4_096
ARBITER_MAX = 8_192
R4_MAX = 16_384
PROBE_MAX = 128
STRIKES_TO_ABORT = 3
CONCURRENCY_RETRIES = 30
EXTRA_WITNESSES = 3
DEFAULT_SANDBOX_IMAGE = "eddie-sandbox:local"


class StopRun(Exception):
    def __init__(self, reason: str, *, hard: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.hard = hard


class StepFailed(Exception):
    pass


@dataclass
class RunOptions:
    phase: str
    dry_run: bool = False
    concurrency: int = 3
    only: str | None = None
    max_calls: int | None = None
    calibration_pair: bool = True
    triage_effort: str = "low"
    arbiter_effort: str = "high"
    structured: bool | None = None  # None: probe and remember
    force: bool = False
    sandbox: str | None = None  # docker | process (default: docker for ember, process otherwise)
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE
    batch_size: int = 18
    db: Path | None = None


@dataclass
class RunPaths:
    cone: Path
    index: Path
    plan: Path
    db: Path
    journal: Path
    artifacts: Path
    lock: Path

    @classmethod
    def for_settings(cls, settings: Settings, *, live: bool, db: Path | None = None) -> RunPaths:
        data = settings.data_dir
        state = data / "state"
        return cls(
            cone=data / "index" / "eddie_cone.sqlite",
            index=settings.index_path,
            plan=data / "index" / "work_plan.json",
            db=db or (state / ("eddie_artifacts.sqlite" if live else "rehearsal.sqlite")),
            journal=state / ("ember_responses" if live else "rehearsal_responses"),
            artifacts=data / "artifacts",
            lock=state / "run-ember.lock",
        )


@dataclass
class Summary:
    phase: str
    stopped: str | None = None
    hard: bool = False
    calls: int = 0
    cost_nano: int = 0
    steps: dict[str, dict[str, int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if not self.stopped else (4 if self.hard else 3)


def source_for(backend: str) -> str:
    return {"ember": "ember", "dgx": "dgx"}.get(backend, "mock")


def batch_key(slug: str, decls: list[DeclRef]) -> str:
    digest = hashlib.sha256(",".join(str(d.id) for d in decls).encode()).hexdigest()[:10]
    return f"triage:{slug}:{digest}"


def ledger_path_for(settings: Settings) -> Path:
    return settings.ledger_path if settings.backend == "ember" else settings.shadow_ledger_path


def phase_spend_nano(ledger: Path, phase: str) -> int:
    """What this phase's calls cost so far, including any still reserved by a dead run."""
    if not ledger.exists():
        return 0
    con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN state IN ('settled', 'orphan_charged') THEN actual_nano END), 0), "
            "COALESCE(SUM(CASE WHEN state IN ('reserved', 'dispatched') THEN reserved_nano END), 0) "
            "FROM calls WHERE tag LIKE ?",
            (phase_tag_like(phase),),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()
    return int(row[0]) + int(row[1])


@contextlib.contextmanager
def run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "w")  # noqa: SIM115 - held for the whole run
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StopRun("another run-ember is already running on this machine", hard=True) from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def usd(nano: int) -> str:
    return f"${nano / NANO_PER_USD:.4f}"


class RunContext:
    """Everything a step needs: the caller, the store (through one writer), the sandbox, the stop flag."""

    def __init__(self, *, settings: Settings, gateway: EddieLLMGateway, store: ArtifactStore, paths: RunPaths,
                 opts: RunOptions, selection: Selection, sandbox: Any, log: Callable[[str], None], structured: bool) -> None:
        self.settings = settings
        self.gateway = gateway
        self.store = store
        self.paths = paths
        self.opts = opts
        self.selection = selection
        self.log = log
        self.source = source_for(gateway.backend)
        self.journal = Journal(paths.journal)
        self.structured = structured
        self.caller = Caller(gateway, journal=self.journal, phase=opts.phase, structured=structured, record=self._record)
        self.runner = sandbox
        self.sem = asyncio.Semaphore(max(1, opts.concurrency))
        self.sandbox_sem = asyncio.Semaphore(2)
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eddie-store")
        self.stopping: str | None = None
        self.hard = False
        self.strikes = 0
        self.calls = 0
        self.cost_nano = 0
        self.probing_structured = False
        self.extras_left = EXTRA_WITNESSES
        self._reused: set[str] = set()
        self._facts: dict[str, dict[str, Any]] | None = None

    # --- plumbing -------------------------------------------------------------------------------

    async def write(self, fn: Callable[[], Any]) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._writer, fn)

    def close(self) -> None:
        self._writer.shutdown(wait=True)

    def stop(self, reason: str, *, hard: bool) -> None:
        if self.stopping is None or (hard and not self.hard):
            self.stopping, self.hard = reason, hard
            self.log(f"stopping: {reason}" + ("" if hard else " (in-flight calls are allowed to finish)"))

    async def _record(self, **row: Any) -> None:
        await self.write(lambda: self.store.record_call(**row))

    async def activity(self, region: str | None, decl: str | None, message: str) -> None:
        await self.write(lambda: self.store.set_activity(region, decl_full_name=decl, message=message, source=self.source))

    def cone(self) -> sqlite3.Connection:
        return open_cone(self.paths.cone, self.paths.index)

    def facts(self, slug: str) -> dict[str, Any]:
        """The cone's structural facts for one chapter (computed once per run: it scans every pillar)."""
        if self._facts is None:
            con = self.cone()
            try:
                self._facts = {f["slug"]: f for f in chapter_facts(con)}
            finally:
                con.close()
        return self._facts[slug]

    # --- the paid call, idempotent per slot -----------------------------------------------------

    def _ledger_row(self, tag: str) -> sqlite3.Row | None:
        with self.gateway.governor.ledger.read() as con:
            return con.execute("SELECT * FROM calls WHERE tag = ? ORDER BY created_at DESC LIMIT 1", (tag,)).fetchone()

    def _recover(self, tag: str, schema: str) -> CallResult | None:
        row = self._ledger_row(tag)
        if row is None or row["state"] != "settled":
            return None
        entry = self.journal.read(row["id"])
        if entry is None or not isinstance(entry.get("payload"), dict):
            return None
        payload = entry["payload"]
        text, summaries = output_text(payload)
        resp = EddieResponse(
            call_id=row["id"], backend=row["backend"], model=str(payload.get("model") or ""), brain_region=row["region"],
            effort=row["effort"], status=str(payload.get("status") or ""), text=text,
            usage=Usage(row["input_tokens"] or 0, row["cached_tokens"] or 0, row["output_tokens"] or 0,
                        row["reasoning_tokens"] or 0),
            cost_nano=row["actual_nano"] or 0, shadow_nano=row["shadow_nano"] or 0, latency_ms=row["latency_ms"] or 0,
            response_id=payload.get("id"), reasoning_summary=summaries,
        )
        return interpret(resp, schema=schema, tag=tag)

    async def call_slot(self, slot_key: str, *, region: str, effort: str, max_output: int, instructions: str,
                        parts: list[dict[str, Any]], schema: str, summary: str | None = None,
                        retries: int = 0) -> CallResult:
        res: CallResult | None = None
        for _ in range(retries + 1):
            res = await self._slot_once(slot_key, region=region, effort=effort, max_output=max_output,
                                        instructions=instructions, parts=parts, schema=schema, summary=summary)
            if res.ok:
                break
        assert res is not None
        status = "done" if res.ok else "failed"
        await self.write(lambda: self.store.mark_step(slot_key, status, result={"call_id": res.call_id, "status": res.status},
                                                      error=None if res.ok else res.error))
        return res

    async def _slot_once(self, slot_key: str, *, region: str, effort: str, max_output: int, instructions: str,
                         parts: list[dict[str, Any]], schema: str, summary: str | None) -> CallResult:
        slot = await self.write(lambda: self.store.ensure_step(
            phase=self.opts.phase, step_key=slot_key, tag=make_tag(self.opts.phase, slot_key, 0), kind="call",
            source=self.source))
        k = int(slot["attempts"])
        if k > 0 and slot["status"] != "failed" and slot_key not in self._reused:
            # A settled, journaled answer from an earlier run (or from before a crash) is reused, never
            # paid for again. A slot is read back at most once per run, and never once this run has
            # called it: a retry after an unusable answer is a fresh call, not the same answer again.
            recovered = await asyncio.to_thread(self._recover, make_tag(self.opts.phase, slot_key, k - 1), schema)
            if recovered is not None:
                self._reused.add(slot_key)
                return recovered
        if self.stopping:
            raise StopRun(self.stopping, hard=self.hard)
        if self.opts.max_calls is not None and self.calls >= self.opts.max_calls:
            self.stop(f"--max-calls {self.opts.max_calls} reached", hard=False)
            raise StopRun(self.stopping or "", hard=False)
        await self.write(lambda: self.store.mark_step(slot_key, "running", bump_attempt=True))
        req = self.caller.build(step_key=slot_key, attempt=k, region=region, effort=effort, max_output=max_output,
                                instructions=instructions, parts=parts, schema=schema, summary=summary)
        res = await self._send(req, slot_key, schema)
        self._reused.add(slot_key)
        self.calls += 1
        self.cost_nano += res.cost_nano
        if res.status == "completed":
            self.strikes = 0
        else:
            self._strike(f"{region} returned {res.status}: {res.error}")
        return res

    def _strike(self, why: str) -> None:
        self.strikes += 1
        self.log(f"strike {self.strikes}/{STRIKES_TO_ABORT}: {why}")
        if self.strikes >= STRIKES_TO_ABORT:
            self.stop(f"{STRIKES_TO_ABORT} failed calls in a row (last: {why})", hard=True)

    async def _send(self, req: Any, slot_key: str, schema: str) -> CallResult:
        for _ in range(CONCURRENCY_RETRIES):
            try:
                async with self.sem:
                    return await self.caller.call(req, step_key=slot_key, schema=schema)
            except BudgetExceededError as exc:
                if exc.kind == "concurrency":  # refused before any I/O: wait for a slot
                    await asyncio.sleep(2.0)
                    continue
                hard = exc.kind not in ("run", "daily")
                self.stop(f"{exc}", hard=hard)
                raise StopRun(str(exc), hard=hard) from exc
            except (CircuitOpenError, KillSwitchError, LedgerIntegrityError, TransportGuardError,
                    LiveEmberDisabledError) as exc:
                self.stop(f"{type(exc).__name__}: {exc}", hard=True)
                raise StopRun(str(exc), hard=True) from exc
            except UpstreamError as exc:
                if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
                    if self.probing_structured and exc.status == 400:
                        raise
                    self.stop(f"HTTP {exc.status} from the model ({exc}); aborting the phase", hard=True)
                    raise StopRun(str(exc), hard=True) from exc
                self._strike(f"upstream error: {exc}")
                raise StepFailed(str(exc)) from exc
            except GuardError as exc:  # refused before any I/O (too large, unbounded, policy): this step only
                raise StepFailed(f"refused before sending: {exc}") from exc
        self.stop("the ledger stayed at its concurrency limit for a minute", hard=True)
        raise StopRun(self.stopping or "", hard=True)

    # --- reads and writes for steps -------------------------------------------------------------

    async def attempts(self, decl: str) -> list[sqlite3.Row]:
        def read() -> list[sqlite3.Row]:
            with self.store.read() as con:
                return con.execute("SELECT * FROM witness_attempts WHERE decl_full_name = ? AND source = ? ORDER BY attempt, id",
                                   (decl, self.source)).fetchall()

        return await self.write(read)

    async def last_witness(self, decl: str) -> int | None:
        def read() -> int | None:
            with self.store.read() as con:
                row = con.execute("SELECT MAX(id) FROM witnesses WHERE decl_full_name = ? AND source = ?",
                                  (decl, self.source)).fetchone()
            return row[0] if row else None

        return await self.write(read)

    async def write_attempt(self, *, decl: str, attempt: int, origin: str, effort: str, outcome: str,
                            critique: list[str] | None, res: CallResult, out: Any = None, stderr: str | None = None,
                            qc: dict | None = None, artifact: Any = None, sandbox_job: str | None = None,
                            supersedes: int | None = None, witness_status: str | None = None,
                            critic_verdict: dict | None = None, critic: CallResult | None = None) -> None:
        decl_id = next((d.id for p in self.selection.pillars for d in p.triage if d.full_name == decl), None)

        def tx() -> None:
            with self.store.tx() as con:
                wid = None
                if artifact is not None:
                    defects = list(qc.get("defects", [])) if outcome == "qc_failed" and qc else (
                        list(critic_verdict.get("defects", [])) if critic_verdict and outcome == "rejected" else [])
                    wid = self.store.add_witness(
                        decl, kind="plot", artifact_sha256=artifact.sha256, ext="png", source=self.source,
                        status=witness_status or "pending", title=out.title if out else None, decl_id=decl_id,
                        bytes_=artifact.bytes, python_source=out.python_source if out else None, sandbox_job_id=sandbox_job,
                        qc=qc, defects=defects, critic_verdict=critic_verdict, supersedes_id=supersedes, pair_id=supersedes,
                        call_id=res.call_id, response_id=res.response_id, attempt=attempt, origin=origin,
                        caption=out.caption if out else None, relation=out.relation if out else None, con=con)
                    if critic is not None and critic.ok:
                        v = critic.data
                        self.store.add_review(region="R2.critic", verdict=v.verdict, source=self.source, witness_id=wid,
                                              decl_full_name=decl, reasons=[*v.defects, *(f"fix: {f}" for f in v.fixes), v.summary],
                                              call_id=critic.call_id, response_id=critic.response_id, con=con)
                self.store.add_attempt(
                    decl_full_name=decl, attempt=attempt, origin=origin, outcome=outcome, source=self.source, effort=effort,
                    title=out.title if out else None, caption=out.caption if out else None,
                    relation=out.relation if out else None, python_source=out.python_source if out else None,
                    stderr_tail=stderr, qc=qc, critique=critique, witness_id=wid,
                    call_id=res.call_id if res.response is not None else None,
                    response_id=res.response_id if res.response is not None else None,
                    critic_call_id=critic.call_id if critic else None,
                    critic_response_id=critic.response_id if critic else None, con=con)

        if self.source == "ember" and res.response is not None and not res.response_id:
            raise StepFailed("an Ember reply without a response id cannot be stored as Ember output")
        await self.write(tx)

    async def sandbox(self, script: str) -> Any:
        async with self.sandbox_sem:
            return await self.runner.run(script, timeout_s=60)


# --- steps ------------------------------------------------------------------------------------------


def _triage_prompt(con: sqlite3.Connection, batch: list[DeclRef]) -> tuple[str, list[str]]:
    lines: list[str] = []
    seen_modules: set[str] = set()
    ids = []
    for i, d in enumerate(batch, 1):
        c = card(con, d.id)
        label = f"d{i}"
        ids.append(label)
        with_ctx = c.module not in seen_modules
        seen_modules.add(c.module)
        lines.append(render(c, label=label, with_context=with_ctx))
        lines.append("")
    return "\n".join(lines).strip(), ids


async def triage_batch(ctx: RunContext, plan: PillarPlan, batch: list[DeclRef], *, depth: int = 0) -> None:
    key = batch_key(plan.slug, batch)
    step = await ctx.write(lambda: ctx.store.ensure_step(phase=ctx.opts.phase, step_key=key, tag=key, kind="triage",
                                                         pillar_slug=plan.slug, source=ctx.source,
                                                         payload={"decl_ids": [d.id for d in batch]}))
    if step["status"] == "done":
        return
    con = ctx.cone()
    try:
        text, ids = _triage_prompt(con, batch)
    finally:
        con.close()
    await ctx.activity("R1.triage", batch[0].full_name, f"triaging {len(batch)} declarations of {short_title(plan.title)}")
    try:
        res = await ctx.call_slot(f"{key}:call", region="R1.triage", effort=ctx.opts.triage_effort, max_output=TRIAGE_MAX,
                                  instructions=prompts.TRIAGE, parts=[text_part(text)], schema="triage")
    except StepFailed as exc:
        error = str(exc)
        await ctx.write(lambda: ctx.store.mark_step(key, "failed", error=error))
        return
    if not res.ok:
        await ctx.write(lambda: ctx.store.mark_step(key, "failed", error=res.error))
        if len(batch) > 1 and depth == 0:  # a reply too long or malformed: halve it once
            mid = len(batch) // 2
            await triage_batch(ctx, plan, batch[:mid], depth=1)
            await triage_batch(ctx, plan, batch[mid:], depth=1)
        return
    found = res.data.by_id(ids)
    missing = [d for label, d in zip(ids, batch, strict=True) if label not in found]

    def tx() -> None:
        with ctx.store.tx() as c:
            for label, d in zip(ids, batch, strict=True):
                item = found.get(label)
                if item is None:
                    continue
                ctx.store.put_triage(d.full_name, item.plain_english, source=ctx.source, decl_id=d.id, call_id=res.call_id,
                                     response_id=res.response_id, role=item.role, area=item.area,
                                     visualizability=item.visualizability, visual_hint=item.visual_hint, con=c)
            ctx.store.mark_step(key, "done", result={"triaged": len(found), "missing": len(missing)}, con=c)

    await ctx.write(tx)
    if missing and depth == 0:
        await triage_batch(ctx, plan, missing, depth=1)


async def triage_pillar(ctx: RunContext, plan: PillarPlan) -> None:
    def done_names() -> set[str]:
        with ctx.store.read() as con:
            return {r[0] for r in con.execute("SELECT decl_full_name FROM triage WHERE source = ?", (ctx.source,))}

    already = await ctx.write(done_names)
    todo = [d for d in plan.triage if d.full_name not in already]
    size = max(1, ctx.opts.batch_size)
    for i in range(0, len(todo), size):
        await triage_batch(ctx, plan, todo[i : i + size])


async def _gloss(ctx: RunContext, name: str) -> dict[str, Any] | None:
    def read() -> dict[str, Any] | None:
        with ctx.store.read() as con:
            row = con.execute("SELECT gloss, role, visualizability FROM triage WHERE decl_full_name = ?", (name,)).fetchone()
        return dict(row) if row else None

    return await ctx.write(read)


async def choose_flagships(ctx: RunContext, plan: PillarPlan, *, calibrate: bool) -> list[DeclRef]:
    if calibrate:
        picks = [d for d in (ctx.selection.main_theorem, plan.gate) if d is not None]
        return list({d.id: d for d in picks}.values())
    pool = list({d.id: d for d in [*plan.chokepoints, *([plan.gate] if plan.gate else [])]}.values())
    if not pool:
        return []
    scored = []
    for d in pool:
        g = await _gloss(ctx, d.full_name)
        scored.append(((g or {}).get("visualizability") or 0, d.dom_mass, -d.id, d))
    scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
    picks = [scored[0][3]]
    if ctx.extras_left > 0 and len(scored) > 1 and scored[1][0] >= 3:
        ctx.extras_left -= 1
        picks.append(scored[1][3])
    return picks


def awaiting_review(step: sqlite3.Row) -> bool:
    """A witness step that ended on a render R2 never judged (its review was cut off) is reopened."""
    try:
        return bool(json.loads(step["result"] or "{}").get("pending"))
    except (TypeError, ValueError):
        return False


async def _witness_job(ctx: RunContext, plan: PillarPlan, target: DeclRef, *, calibration_draft: bool) -> WitnessJob:
    gloss = ((await _gloss(ctx, target.full_name)) or {}).get("gloss")
    con = ctx.cone()
    try:
        return WitnessJob(f"witness:{target.full_name}", plan.slug, card(con, target.id), gloss,
                          neighbours(con, target.id), calibration_draft=calibration_draft)
    finally:
        con.close()


async def witness(ctx: RunContext, plan: PillarPlan, target: DeclRef, *, calibration_draft: bool) -> None:
    key = f"witness:{target.full_name}"
    step = await ctx.write(lambda: ctx.store.ensure_step(phase=ctx.opts.phase, step_key=key, tag=key, kind="witness",
                                                         pillar_slug=plan.slug, decl_full_name=target.full_name,
                                                         source=ctx.source))
    if step["status"] == "done" and not awaiting_review(step):
        return
    job = await _witness_job(ctx, plan, target, calibration_draft=calibration_draft)
    await ctx.write(lambda: ctx.store.mark_step(key, "running"))
    try:
        result = await run_witness(ctx, job)
    except StepFailed as exc:
        error = str(exc)
        await ctx.write(lambda: ctx.store.mark_step(key, "failed", error=error))
        return
    await ctx.write(lambda: ctx.store.mark_step(key, "done", result=result))


def unverified_flagships(con: sqlite3.Connection, source: str) -> list[tuple[str, str]]:
    """(chapter slug, declaration) of every finished witness step whose declaration has no verified render."""
    verified = {r[0] for r in con.execute(
        "SELECT DISTINCT decl_full_name FROM witness_attempts WHERE source = ? AND outcome = 'verified'", (source,))}
    return [(r[0], r[1]) for r in con.execute(
        "SELECT pillar_slug, decl_full_name FROM pipeline_steps WHERE kind = 'witness' AND status = 'done' AND source = ? "
        "ORDER BY id", (source,)) if r[1] not in verified]


def flagship_ref(sel: Selection, plan: PillarPlan, decl: str) -> DeclRef | None:
    pool = [*plan.chokepoints, *([plan.gate] if plan.gate else []), *plan.triage,
            *([sel.main_theorem] if sel.main_theorem else [])]
    return next((d for d in pool if d.full_name == decl), None)


async def repair_witness(ctx: RunContext, plan: PillarPlan, target: DeclRef) -> bool:
    """Up to REPAIR_ATTEMPTS more high-effort attempts for one flagship; True when it ends verified."""
    key = f"repair:{target.full_name}"
    step = await ctx.write(lambda: ctx.store.ensure_step(phase=ctx.opts.phase, step_key=key, tag=key, kind="repair",
                                                         pillar_slug=plan.slug, decl_full_name=target.full_name,
                                                         source=ctx.source))
    if step["status"] == "done" and not awaiting_review(step):
        return bool(json.loads(step["result"] or "{}").get("verified"))
    prior = await ctx.attempts(target.full_name)
    draft = any(a["origin"] == "calibration_draft" for a in prior)  # keeps the attempt numbering of the first run
    job = await _witness_job(ctx, plan, target, calibration_draft=draft)
    await ctx.write(lambda: ctx.store.mark_step(key, "running"))
    try:
        result = await run_witness(ctx, job, extra=REPAIR_ATTEMPTS)
    except StepFailed as exc:
        error = str(exc)
        await ctx.write(lambda: ctx.store.mark_step(key, "failed", error=error))
        return False
    await ctx.write(lambda: ctx.store.mark_step(key, "done", result=result))
    return bool(result.get("verified"))


async def repair(ctx: RunContext) -> None:
    """The repair phase: more high-effort attempts for every flagship that still has no verified witness.

    A chapter whose flagship verifies here is written again by R4: the chapter in place was written around a
    figure R2 had rejected. Pillars run concurrently; within one, flagships go in turn and R4 runs once.
    """
    def read() -> tuple[list[tuple[str, str]], set[str]]:
        with ctx.store.read() as con:
            todo = unverified_flagships(con, ctx.source)
            owed = {r[0] for r in con.execute(  # repaired in an earlier run that stopped before its chapter
                "SELECT pillar_slug, result FROM pipeline_steps WHERE kind = 'repair' AND status = 'done' AND source = ?",
                (ctx.source,)) if json.loads(r[1] or "{}").get("verified")}
        return todo, owed

    todo, owed = await ctx.write(read)
    by_slug: dict[str, list[str]] = {slug: [] for slug in owed}
    for slug, decl in todo:
        by_slug.setdefault(slug, []).append(decl)
    if ctx.opts.only:
        by_slug = {s: d for s, d in by_slug.items() if s == ctx.opts.only}
    plans = {p.slug: p for p in ctx.selection.pillars}

    async def pillar(slug: str, decls: list[str]) -> None:
        plan = plans.get(slug)
        if plan is None:
            ctx.log(f"repair: chapter {slug} is not in the current selection; skipped")
            return
        verified = slug in owed
        for decl in decls:
            target = flagship_ref(ctx.selection, plan, decl)
            if target is None:
                ctx.log(f"repair: {decl} is not in chapter {slug} of the current selection; skipped")
                continue
            verified = await repair_witness(ctx, plan, target) or verified
        if verified:
            await consolidate(ctx, plan, key=f"r4:{slug}:repair")

    async def guarded(slug: str, decls: list[str]) -> None:
        try:
            await pillar(slug, decls)
        except StopRun:
            pass  # the flag is set; every other pillar stops at its next call

    await asyncio.gather(*(guarded(s, d) for s, d in by_slug.items()))


async def arbiter(ctx: RunContext, plan: PillarPlan, target: DeclRef) -> None:
    key = f"arbiter:{target.full_name}"
    step = await ctx.write(lambda: ctx.store.ensure_step(phase=ctx.opts.phase, step_key=key, tag=key, kind="arbiter",
                                                         pillar_slug=plan.slug, decl_full_name=target.full_name,
                                                         source=ctx.source))
    if step["status"] == "done":
        return
    con = ctx.cone()
    try:
        tc = card(con, target.id)
        nbs = neighbours(con, target.id)
        body = body_excerpt(con, target.id)
    finally:
        con.close()
    gloss = (await _gloss(ctx, target.full_name)) or {}

    def latest_witness() -> dict[str, Any] | None:
        with ctx.store.read() as c:
            row = c.execute("SELECT title, caption, relation, status, critic_verdict FROM witnesses WHERE decl_full_name = ? "
                            "AND source = ? ORDER BY (status = 'verified') DESC, id DESC LIMIT 1",
                            (target.full_name, ctx.source)).fetchone()
        return dict(row) if row else None

    w = await ctx.write(latest_witness)
    allowed = {tc.full_name, *(n.full_name for n in nbs)}
    parts = ["Declaration to audit:", render(tc, label="target")]
    if body:
        parts += ["Excerpt of its proof:", "```lean", body, "```"]
    parts += ["", "It uses (module and line):", *(render(n, with_context=False) for n in nbs)]
    parts += ["", f"First reader's gloss: {gloss.get('gloss') or '(none)'}"]
    if w:
        summary = (json.loads(w["critic_verdict"]).get("summary") if w["critic_verdict"] else None) or ""
        parts += [f"Witness ({w['status']}): {w['title']}. {w['caption']} (relation: {w['relation']}). {summary}"]
    parts += ["", "Names you may cite: " + ", ".join(sorted(allowed))]
    await ctx.activity("R3.arbiter", target.full_name, f"auditing {target.full_name}")
    try:
        res = await ctx.call_slot(f"{key}:call", region="R3.arbiter", effort=ctx.opts.arbiter_effort,
                                  max_output=ARBITER_MAX, instructions=prompts.ARBITER,
                                  parts=[text_part("\n".join(parts))], schema="arbiter", summary="concise", retries=1)
    except StepFailed as exc:
        error = str(exc)
        await ctx.write(lambda: ctx.store.mark_step(key, "failed", error=error))
        return
    if not res.ok:
        await ctx.write(lambda: ctx.store.mark_step(key, "failed", error=res.error))
        return
    a = res.data
    intuition, notes = prose(a.intuition_md, allowed)
    by_name = {c.full_name: c for c in [tc, *nbs]}
    cited = [by_name[n] for n in dict.fromkeys(a.citations) if n in by_name]
    dropped = [n for n in a.citations if n not in by_name]
    citation = [{"decl": c.full_name, "module": c.module, "path": f"src/{c.path}", "start_line": c.start_line,
                 "end_line": c.end_line} for c in cited]
    arbiter_notes = json.dumps({
        "verdict": a.verdict, "hypotheses": [h.model_dump() for h in a.hypotheses], "caveats": a.caveats,
        "confidence": a.confidence, "reasoning_summary": res.response.reasoning_summary if res.response else [],
        "dropped_citations": dropped, "notes": notes,
    })

    def tx() -> None:
        with ctx.store.tx() as c:
            ctx.store.put_intuition(target.full_name, intuition, source=ctx.source, decl_id=target.id,
                                    formal_signature=tc.signature, citation=citation,
                                    verdict="rejected" if a.verdict == "unfaithful" else "verified",
                                    arbiter_notes=arbiter_notes, call_id=res.call_id, response_id=res.response_id, con=c)
            ctx.store.add_review(region="R3.arbiter", verdict=a.verdict, source=ctx.source, decl_full_name=target.full_name,
                                 reasons=a.caveats, call_id=res.call_id, response_id=res.response_id, con=c)
            ctx.store.mark_step(key, "done", result={"verdict": a.verdict, "citations": len(citation)}, con=c)

    await ctx.write(tx)


def _facts_text(f: dict[str, Any]) -> str:
    lines = [
        f"Chapter {f['chapter']} of {f['chapters']}: {f['title']} ({f['tier_label']}).",
        f"{f['live']:,} live declarations in {f['modules']:,} modules; {f['dead']:,} more the proof never uses.",
        f"Other chapters build on it {f['ref_in']:,} times; it builds on them {f['ref_out']:,} times.",
    ]
    for label, key in (("Builds on", "builds_on"), ("Feeds", "feeds")):
        if f[key]:
            lines.append(f"{label}: " + "; ".join(f"{short_title(n['title'])} (chapter {n['chapter']}, {n['net']:,} net refs)"
                                                   for n in f[key]))
    if f["final_theorems"]:
        lines.append("Final theorems here: " + ", ".join(f"`{t['full_name']}`" for t in f["final_theorems"]))
    for b in f["bottlenecks"]:
        lines.append(f"Chokepoint `{b['full_name']}`: every route from the final theorem to {max(b['dom_mass'] - 1, 0):,} "
                     f"declarations ({100 * b['dom_share']:.1f}% of the live proof) passes through it. Statement: {b['signature'][:400]}")
    if f["remark_steps"]:
        lines.append("Authors' summary PDF, matched by keyword: " + "; ".join(f"step {s['number']}: {s['phrase']}" for s in f["remark_steps"]))
    return "\n".join(lines)


async def consolidate(ctx: RunContext, plan: PillarPlan, *, key: str | None = None) -> None:
    key = key or f"r4:{plan.slug}"  # the repair phase rewrites a chapter under its own key
    step = await ctx.write(lambda: ctx.store.ensure_step(phase=ctx.opts.phase, step_key=key, tag=key, kind="consolidate",
                                                         pillar_slug=plan.slug, source=ctx.source))
    if step["status"] == "done":
        return
    facts = await asyncio.to_thread(ctx.facts, plan.slug)
    names = [d.full_name for d in plan.triage]

    def gather() -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
        with ctx.store.read() as c:
            marks = ", ".join("?" for _ in names) or "''"
            glosses = [dict(r) for r in c.execute(
                f"SELECT decl_full_name, gloss, role FROM triage WHERE decl_full_name IN ({marks})", names)]
            gate = None
            if plan.gate is not None:
                row = c.execute("SELECT intuition_md, verdict FROM intuitions WHERE decl_full_name = ?",
                                (plan.gate.full_name,)).fetchone()
                gate = dict(row) if row else None
            wit = [dict(r) for r in c.execute(
                f"SELECT decl_full_name, title, caption, relation, status FROM witnesses WHERE decl_full_name IN ({marks}) "
                "AND source = ? ORDER BY (status = 'verified') DESC, id DESC LIMIT 3", [*names, ctx.source])]
        return glosses, gate, wit

    glosses, gate, wits = await ctx.write(gather)
    order = {n: i for i, n in enumerate(names)}
    glosses.sort(key=lambda g: order.get(g["decl_full_name"], 1 << 30))
    allowed = set(names) | {b["full_name"] for b in facts["bottlenecks"]} | {t["full_name"] for t in facts["final_theorems"]}
    if facts["head"]["full_name"]:
        allowed.add(facts["head"]["full_name"])
    parts = ["Measured structure of this chapter:", _facts_text(facts), "", "What its key declarations do (first readers' glosses):"]
    parts += [f"- `{g['decl_full_name']}` ({g['role'] or 'declaration'}): {g['gloss']}" for g in glosses[:40]]
    if gate:
        parts += ["", f"Audited intuition of the gate theorem `{plan.gate.full_name}` ({gate['verdict']}):",
                  clip(gate["intuition_md"], CARRY_INTUITION)]
    for w in wits:
        parts += ["", f"Witness for `{w['decl_full_name']}` ({w['status']} by the visual critic): {w['title']}. "
                  f"{w['caption']} (relation: {w['relation']})"]
    parts += ["", "Allowed Lean names: " + ", ".join(sorted(allowed))]
    text = "\n".join(parts)
    await ctx.activity("R4.consolidator", plan.gate.full_name if plan.gate else None, f"writing chapter {plan.chapter}")
    best = None
    for attempt in range(2):
        why = "; ".join(best[2]) if best else "the previous reply could not be used: return the JSON object"
        prompt = text if attempt == 0 else text + "\n\nYour previous draft had these problems; fix them: " + why
        try:
            res = await ctx.call_slot(f"{key}:call{attempt}", region="R4.consolidator", effort="high" if attempt == 0 else "medium",
                                      max_output=R4_MAX, instructions=prompts.CONSOLIDATOR, parts=[text_part(prompt)],
                                      schema="chapter")
        except StepFailed as exc:
            error = str(exc)
            await ctx.write(lambda e=error: ctx.store.mark_step(key, "failed", error=e))
            return
        if not res.ok:
            continue
        md, notes = prose(res.data.synthesis_md, allowed)
        problems = chapter_problems(md)
        best = (res, md, problems, notes)
        if not problems:
            break
    if best is None or len(best[1].split()) < 400:
        await ctx.write(lambda: ctx.store.mark_step(key, "failed", error="no usable chapter came back"))
        return
    res, md, problems, notes = best

    def tx() -> None:
        with ctx.store.tx() as c:
            ctx.store.put_chapter(plan.slug, plan.title, md, source=ctx.source, call_id=res.call_id,
                                  response_id=res.response_id, cost_nano=res.cost_nano, headline=res.data.headline, con=c)
            ctx.store.mark_step(key, "done", result={"words": len(md.split()), "problems": problems, "notes": notes}, con=c)

    await ctx.write(tx)


# --- probes and phases -----------------------------------------------------------------------------


def probe_png() -> bytes:
    import io

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    fig = Figure(figsize=(3.2, 2.0), dpi=100)
    ax = fig.add_subplot()
    ax.plot([0, 1, 2, 3], [0, 1, 0, 1])
    ax.set_title("probe")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


PROBE_DECL = "theorem demo_add_zero (n : Nat) : n + 0 = n"


async def run_probes(ctx: RunContext) -> None:
    """Send each region's exact request shape once, tiny and capped at 128 output tokens."""
    shapes = [
        ("triage", "R1.triage", ctx.opts.triage_effort, prompts.TRIAGE, [text_part(f"[d1] {PROBE_DECL}")], None),
        ("worker", "R1.worker", "medium", prompts.WORKER, [text_part(f"Declaration: {PROBE_DECL}")], None),
        ("critic", "R2.critic", "medium", prompts.CRITIC, [text_part("Title: probe. Caption: a zigzag."), {"type": "image", "data": probe_png(), "mime": "image/png"}], None),
        ("arbiter", "R3.arbiter", ctx.opts.arbiter_effort, prompts.ARBITER, [text_part(f"Declaration: {PROBE_DECL}")], "concise"),
        ("chapter", "R4.consolidator", "high", prompts.CONSOLIDATOR, [text_part("Chapter: probe.")], None),
    ]
    for i, (schema, region, effort, instructions, parts, summary) in enumerate(shapes):
        key = f"probe:{schema}:{'s' if ctx.structured else 'p'}"
        ctx.probing_structured = i == 0 and ctx.structured
        try:
            res = await ctx._slot_once(key, region=region, effort=effort, max_output=PROBE_MAX, instructions=instructions,
                                       parts=parts, schema=schema, summary=summary)
        except UpstreamError as exc:
            # F4: a 400 on the json_schema probe switches the run to prompt-JSON mode instead of aborting.
            ctx.probing_structured = False
            await ctx.write(lambda k=key, e=exc: ctx.store.mark_step(k, "failed", error=f"HTTP {e.status}: {e}"))
            ctx.log(f"notice: the model refused structured output (HTTP {exc.status}: {exc}); switching this run to "
                    "prompt-JSON mode (the schema rides in the instructions, replies are parsed tolerantly)")
            ctx.structured = False
            ctx.caller.structured = False
            await ctx.write(lambda: ctx.store.set_meta("pipeline.structured", "0"))
            return await run_probes(ctx)
        finally:
            ctx.probing_structured = False
        await ctx.write(lambda k=key, r=res: ctx.store.mark_step(k, "done", result={"status": r.status}))
        ctx.strikes = 0  # a probe may truncate by design; it never counts toward the abort
        ctx.log(f"probe {region:<16} {res.status:<11} in={res.response.usage.input_tokens if res.response else 0} "
                f"out={res.response.usage.output_tokens if res.response else 0} cost {usd(res.cost_nano)}")
    if ctx.structured:
        await ctx.write(lambda: ctx.store.set_meta("pipeline.structured", "1"))


async def run_pillar(ctx: RunContext, plan: PillarPlan, *, calibrate: bool) -> None:
    await triage_pillar(ctx, plan)
    for target in await choose_flagships(ctx, plan, calibrate=calibrate):
        draft = calibrate and ctx.opts.calibration_pair and ctx.selection.main_theorem is not None and \
            target.id == ctx.selection.main_theorem.id
        await witness(ctx, plan, target, calibration_draft=draft)
    gates = [plan.gate] if plan.gate else []
    if calibrate and ctx.selection.main_theorem is not None:
        gates = [ctx.selection.main_theorem, *gates]
    for g in list({d.id: d for d in gates}.values()):
        await arbiter(ctx, plan, g)
    await consolidate(ctx, plan)


async def execute(ctx: RunContext) -> None:
    if ctx.opts.phase == "repair":
        await repair(ctx)
        return
    calibrate = ctx.opts.phase == "calibrate"
    probe_keys = [s["step_key"] for s in ctx.store.steps("calibrate") if s["kind"] == "call" and s["step_key"].startswith("probe:")
                  and s["status"] == "done"]
    if calibrate and len(probe_keys) < 5:
        await run_probes(ctx)
    sel = ctx.selection
    if calibrate:
        plans = [p for p in sel.pillars if p.slug == sel.main_slug]
    else:
        plans = [p for p in sel.pillars if p.slug != sel.main_slug]
    if ctx.opts.only:
        plans = [p for p in plans if p.slug == ctx.opts.only]
        if not plans:
            raise StopRun(f"no chapter {ctx.opts.only!r} in this phase", hard=True)

    async def guarded(plan: PillarPlan) -> None:
        try:
            await run_pillar(ctx, plan, calibrate=calibrate)
        except StopRun:
            pass  # the flag is set; every other pillar stops at its next call

    await asyncio.gather(*(guarded(p) for p in plans))


def summarize(store: ArtifactStore, phase: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in store.steps(phase):
        kind = s["kind"]
        out.setdefault(kind, {})
        out[kind][s["status"]] = out[kind].get(s["status"], 0) + 1
    return out


async def run_phase(settings: Settings, opts: RunOptions, *, sandbox: Any = None,
                    gateway_factory: Callable[[Settings], EddieLLMGateway] | None = None,
                    log: Callable[[str], None] = print) -> Summary:
    if opts.phase not in PHASES:
        raise StopRun(f"unknown phase {opts.phase!r}", hard=True)
    live = settings.backend == "ember"
    paths_ = RunPaths.for_settings(settings, live=live, db=opts.db)
    summary = Summary(opts.phase)
    for need in (paths_.cone, paths_.index):
        if not need.exists():
            raise StopRun(f"missing {need}: build the index and the cone first (make cone)", hard=True)
    with run_lock(paths_.lock):
        store = ArtifactStore(paths_.db)
        selection = select(paths_.cone, paths_.index, plan_path=paths_.plan)
        if opts.phase == "full-atlas" and not opts.force:
            done = store.step(f"r4:{selection.main_slug}")
            if done is None or done["status"] != "done":
                raise StopRun("full-atlas waits for a finished calibrate (chapter 20's R4 chapter); pass --force to skip",
                              hard=True)
        spent = phase_spend_nano(ledger_path_for(settings), opts.phase)
        remaining = settings.run_cap_nano - spent
        log(f"phase {opts.phase}: budget {usd(settings.run_cap_nano)}, already spent by this phase {usd(spent)}, "
            f"remaining {usd(max(remaining, 0))}")
        if remaining <= 0:
            summary.stopped, summary.hard = "this phase's budget is spent", False
            return summary
        run_settings = replace(settings, run_cap_nano=remaining)
        factory = gateway_factory or _default_gateway
        gateway = factory(run_settings)
        if live and getattr(gateway, "key_source", None) == "env":
            await gateway.aclose()
            raise StopRun("the key came through META_API_KEY; run live through scripts/ember-live.sh (pipe hand-off)",
                          hard=True)
        if sandbox is None:
            sandbox = _default_sandbox(settings, opts, live=live)
        stored = store.meta("pipeline.structured")
        structured = opts.structured if opts.structured is not None else (stored != "0")
        ctx = RunContext(settings=run_settings, gateway=gateway, store=store, paths=paths_, opts=opts,
                         selection=selection, sandbox=sandbox, log=log, structured=structured)
        loop = asyncio.get_running_loop()
        main = asyncio.current_task()
        presses = {"n": 0}

        def on_sigint() -> None:
            presses["n"] += 1
            if presses["n"] == 1:
                ctx.stop("interrupted; in-flight calls are settling (Ctrl-C again to abort them)", hard=False)
            elif main is not None:
                main.cancel()

        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGINT, on_sigint)
        try:
            await execute(ctx)
        except StopRun as exc:
            ctx.stop(exc.reason, hard=exc.hard)
        finally:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signal.SIGINT)
            await ctx.write(lambda: store.set_activity(None, message=f"{opts.phase} {'stopped' if ctx.stopping else 'finished'}",
                                                       source=ctx.source))
            ctx.close()
            await gateway.aclose()
        summary.stopped, summary.hard = ctx.stopping, ctx.hard
        summary.calls, summary.cost_nano = ctx.calls, ctx.cost_nano
        summary.steps = summarize(store, opts.phase)
        if not ctx.structured:
            summary.notes.append("ran in prompt-JSON mode (the model refused structured output)")
        if live:
            summary.notes.append(export_telemetry(settings))
        return summary


def _default_gateway(settings: Settings) -> EddieLLMGateway:
    if settings.backend == "mock":
        from eddie.pipeline.rehearsal import respond

        return EddieLLMGateway(settings, mock_responder=respond)
    return EddieLLMGateway(settings)


def _default_sandbox(settings: Settings, opts: RunOptions, *, live: bool) -> Any:
    from eddie.sandbox.runner import Limits, SandboxRunner

    isolation = opts.sandbox or ("docker" if live else "process")
    if live and isolation != "docker":
        raise StopRun("live witnesses run only in the container sandbox (--sandbox docker; make sandbox-image)", hard=True)
    return SandboxRunner(settings.data_dir / "artifacts", limits=Limits(wall_s=60, cpu_s=60), isolation=isolation,
                         image=opts.sandbox_image)


def export_telemetry(settings: Settings) -> str:
    from eddie.llm.governor import caps_from_settings, effective_lifetime_cap_nano
    from eddie.llm.ledger import Ledger
    from eddie.llm.telemetry import build_trace, write_json_atomic

    if not settings.ledger_path.exists():
        return "no ledger: telemetry not exported"
    ledger = Ledger(settings.ledger_path, billable=True, create=False)
    cap = effective_lifetime_cap_nano(caps_from_settings(settings), ledger.totals())
    trace = build_trace(ledger, lifetime_cap_nano=cap)
    out = paths.exports_dir() / "eddie_ember_telemetry.json"
    write_json_atomic(out, trace)
    write_json_atomic(settings.public_dir / "telemetry.json", trace)
    return f"telemetry: {trace['totals']['calls']} billable calls, total ${trace['totals']['total_spend_usd']:.6f} -> {out}"


# --- status and audit --------------------------------------------------------------------------------


def status_report(settings: Settings, *, db: Path | None = None) -> dict[str, Any]:
    """The live database unless --db names another (a rehearsal's); spend always from the real ledger.

    A plain shell has no EDDIE_BACKEND=ember, so keying either choice on the backend showed the mock
    rehearsal and $0 right after a live run.
    """
    live_db = RunPaths.for_settings(settings, live=True).db
    paths_ = RunPaths.for_settings(settings, live=True, db=db)
    kind = "live" if paths_.db.resolve() == live_db.resolve() else "other"
    if not paths_.db.exists():
        return {"db": str(paths_.db), "kind": kind, "phases": {}}
    store = ArtifactStore(paths_.db)  # opening as a writer also rolls back a crashed writer's journal
    report: dict[str, Any] = {"db": str(paths_.db), "kind": kind, "phases": {}}
    for phase in PHASES:
        report["phases"][phase] = {"steps": summarize(store, phase),
                                   "spent_usd": phase_spend_nano(settings.ledger_path, phase) / NANO_PER_USD}
    with store.read() as con:
        report["rows"] = {t: dict(con.execute(f"SELECT source, COUNT(*) FROM {t} GROUP BY source").fetchall())
                          for t in ("triage", "intuitions", "witnesses", "reviews", "chapters", "witness_attempts")}
        report["structured"] = store.meta("pipeline.structured")
    return report


AUDITED = (("triage", "decl_full_name"), ("intuitions", "decl_full_name"), ("witnesses", "id"), ("reviews", "id"),
           ("chapters", "pillar_slug"), ("witness_attempts", "id"))


def audit(settings: Settings, *, db: Path | None = None) -> list[str]:
    """Every Ember row must point at a settled, billable ledger call with the same response id."""
    paths_ = RunPaths.for_settings(settings, live=True, db=db)
    if not paths_.db.exists() or not settings.ledger_path.exists():
        return []
    problems: list[str] = []
    led = sqlite3.connect(f"file:{settings.ledger_path}?mode=ro", uri=True)
    led.row_factory = sqlite3.Row
    art = sqlite3.connect(f"file:{paths_.db}?mode=ro", uri=True)
    art.row_factory = sqlite3.Row
    try:
        for table, key in AUDITED:
            cols = {r[1] for r in art.execute(f"PRAGMA table_info({table})")}
            if "response_id" not in cols:
                continue
            for row in art.execute(f"SELECT {key} AS k, call_id, response_id FROM {table} WHERE source = 'ember'"):
                call = led.execute("SELECT state, billable, response_id, tag FROM calls WHERE id = ?", (row["call_id"],)).fetchone()
                where = f"{table}[{row['k']}]"
                if call is None:
                    problems.append(f"{where}: call {row['call_id']} is not in the ledger")
                elif call["billable"] != 1 or call["state"] != "settled":
                    problems.append(f"{where}: call {row['call_id']} is {call['state']} (billable={call['billable']})")
                elif call["response_id"] != row["response_id"]:
                    problems.append(f"{where}: response id differs from the ledger's")
                elif not (call["tag"] or "").startswith("d3:"):
                    problems.append(f"{where}: call {row['call_id']} carries no pipeline tag")
    finally:
        led.close()
        art.close()
    return problems


def run(settings: Settings, opts: RunOptions, **kw: Any) -> Summary:
    return asyncio.run(run_phase(settings, opts, **kw))


def elapsed(started: float) -> str:
    return f"{time.monotonic() - started:.1f} s"
