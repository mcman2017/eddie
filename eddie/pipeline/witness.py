"""R1.worker -> sandbox -> $0 QC -> R2.critic, with every attempt stored.

An attempt ends in one of: invalid_output (the reply could not be used), rejected_static (the
AST check refused the script), sandbox_error (it did not run or drew no witness.png),
qc_failed ($0 image checks: blank, NaN, tiny), rejected (R2 said no) or verified. Anything but
verified feeds the next attempt: the previous script and exactly what was wrong with it.

A crashed run resumes from the attempts already stored; the call slots under it make every
paid call idempotent.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from eddie.pipeline import prompts
from eddie.pipeline.calls import image_part, text_part
from eddie.pipeline.context import CARRY_CODE, CARRY_CRITIQUE, CARRY_HISTORY, Card, clip, render
from eddie.pipeline.scriptcheck import check_script

if TYPE_CHECKING:
    from eddie.pipeline.orchestrator import RunContext

WORKER_MAX = 12_288
WORKER_MAX_HIGH = 16_384
# The region's ceiling. On the live calibrate run reasoning was 80-100% of every R2 reply, and at
# 2,048 one review spent 2,046 tokens reasoning and was cut off before its verdict.
CRITIC_MAX = 4_096
CRITIC_MAX_SIDE = 1_280
STDERR_TAIL = 1_200


@dataclass
class WitnessJob:
    step_key: str
    pillar_slug: str
    target: Card
    gloss: str | None
    neighbours: list[Card]
    calibration_draft: bool = False


def schedule(calibration_draft: bool) -> list[tuple[str, str, int]]:
    """(origin, effort, max_output) per attempt: medium, medium, then high on the last try."""
    plan = [("calibration_draft", "medium", WORKER_MAX)] if calibration_draft else []
    return plan + [("r1", "medium", WORKER_MAX), ("r1", "medium", WORKER_MAX), ("r1", "high", WORKER_MAX_HIGH)]


def worker_prompt(job: WitnessJob, previous_code: str | None, critique: list[str] | None,
                  history: list[str] | None = None) -> str:
    """The R1.worker prompt. A repair-pass attempt also gets the hint and every earlier defect (`history`)."""
    parts = ["Declaration to illustrate:", render(job.target, label="target")]
    if job.gloss:
        parts += ["", f"First reader's plain-English gloss: {job.gloss}"]
    if job.neighbours:
        parts += ["", "It builds on (for context only):"]
        parts += [render(n, with_context=False) for n in job.neighbours]
    if previous_code is not None:
        if history:
            parts += ["", prompts.REPAIR_PASS]
            wrong, cap, heading = history, CARRY_HISTORY, "What was wrong, in every earlier attempt (fix all of it):"
        else:
            wrong, cap, heading = critique or ["(no detail)"], CARRY_CRITIQUE, "What was wrong:"
        parts += ["", prompts.REPAIR, "Previous script:", "```python", clip(previous_code, CARRY_CODE), "```",
                  heading, clip("\n".join(f"- {c}" for c in wrong), cap)]
    return "\n".join(parts)


def critic_prompt(job: WitnessJob, out: Any, qc: dict[str, Any]) -> str:
    return "\n".join([
        "Declaration the plot is meant to illustrate:", render(job.target, label="target", with_context=False),
        f"First reader's gloss: {job.gloss or '(none)'}",
        f"Title: {out.title}", f"Caption: {out.caption}", f"Claimed relation: {out.relation}",
        f"Eddie's automatic image checks: {json.dumps({k: qc.get(k) for k in ('width', 'height', 'luminance_std', 'defects')})}",
        "The rendered plot is attached.",
    ])


def fit_for_critic(png: bytes) -> bytes:
    """Downscale wide renders so the critic sees about what a viewer sees (and the bound stays small)."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        if im.width <= CRITIC_MAX_SIDE and im.height <= CRITIC_MAX_SIDE:
            return png
        scale = CRITIC_MAX_SIDE / max(im.width, im.height)
        small = im.convert("RGB").resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        small.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


async def re_review(ctx: RunContext, job: WitnessJob, att: sqlite3.Row) -> Any | None:
    """Review a render again whose R2 review was cut off before its verdict.

    The render, its script and its caption stay as they are; only the review is repeated, at CRITIC_MAX.
    R2's answer goes to the same witness and attempt, with its own audited review row. Returns the
    verdict, or None when R2 still gave no usable answer (the render stays pending).
    """
    decl, wid = job.target.full_name, int(att["witness_id"])

    def read() -> sqlite3.Row:
        with ctx.store.read() as con:
            return con.execute("SELECT artifact_sha256, ext FROM witnesses WHERE id = ?", (wid,)).fetchone()

    row = await ctx.write(read)
    png = await asyncio.to_thread((ctx.paths.artifacts / f"{row['artifact_sha256']}.{row['ext']}").read_bytes)
    out = SimpleNamespace(title=att["title"], caption=att["caption"], relation=att["relation"])
    qc = json.loads(att["qc"]) if att["qc"] else {}
    await ctx.activity("R2.critic", decl, f"reviewing witness attempt {att['attempt'] + 1} again")
    cres = await ctx.call_slot(
        f"{job.step_key}:c{att['attempt']}:again", region="R2.critic", effort="medium", max_output=CRITIC_MAX,
        instructions=prompts.CRITIC, parts=[text_part(critic_prompt(job, out, qc)), image_part(fit_for_critic(png))],
        schema="critic", retries=1,
    )
    if not cres.ok:
        return None
    if ctx.source == "ember" and not cres.response_id:
        from eddie.pipeline.orchestrator import StepFailed  # the orchestrator imports this module

        raise StepFailed("an Ember reply without a response id cannot be stored as Ember output")
    v = cres.data
    critique = [*v.defects, *(f"fix: {f}" for f in v.fixes)] or None

    def tx() -> None:
        with ctx.store.tx() as con:
            ctx.store.update_witness(wid, status=v.verdict, defects=list(v.defects) if v.verdict == "rejected" else [],
                                     critic_verdict=v.model_dump(), con=con)
            ctx.store.add_review(region="R2.critic", verdict=v.verdict, source=ctx.source, witness_id=wid,
                                 decl_full_name=decl, reasons=[*v.defects, *(f"fix: {f}" for f in v.fixes), v.summary],
                                 call_id=cres.call_id, response_id=cres.response_id, con=con)
            ctx.store.review_attempt(int(att["id"]), outcome=v.verdict, critique=critique, critic_call_id=cres.call_id,
                                     critic_response_id=cres.response_id, con=con)

    await ctx.write(tx)
    return v


def defects_of(attempts: list[Any]) -> list[str]:
    """Every defect R2 or the checks named in these attempts, in order, each once (no-verdict notes left out)."""
    seen: dict[str, None] = {}
    for a in attempts:
        if a["outcome"] in ("verified", "critic_unusable") or not a["critique"]:
            continue
        for c in json.loads(a["critique"]):
            seen.setdefault(str(c), None)
    return list(seen)


async def run_witness(ctx: RunContext, job: WitnessJob, *, extra: int = 0) -> dict[str, Any]:
    """Draw, check and review a witness until R2 verifies one or the schedule runs out.

    `extra` adds that many high-effort repair attempts after the usual schedule (the repair phase); they
    see every defect of every earlier attempt.
    """
    decl = job.target.full_name
    prior = await ctx.attempts(decl)
    if any(a["outcome"] == "verified" for a in prior):
        return {"verified": next(a["witness_id"] for a in prior if a["outcome"] == "verified"), "attempts": len(prior)}
    previous_code: str | None = None
    critique: list[str] | None = None
    previous_witness: int | None = None
    for a in prior:  # resume from the last stored attempt
        previous_code = a["python_source"] or previous_code
        critique = json.loads(a["critique"]) if a["critique"] else critique
        previous_witness = a["witness_id"] or previous_witness
    last = prior[-1] if prior else None
    if last is not None and last["outcome"] == "critic_unusable" and last["witness_id"] is not None:
        # the last render was never judged (its review was cut off): review it before drawing anything new
        verdict = await re_review(ctx, job, last)
        if verdict is None:
            return {"verified": None, "pending": last["witness_id"], "attempts": len(prior)}
        if verdict.verdict == "verified":
            return {"verified": last["witness_id"], "attempts": len(prior)}
        critique = [*verdict.defects, *(f"fix: {f}" for f in verdict.fixes)] or None  # repaired as usual
    plan = schedule(job.calibration_draft) + [("repair", "high", WORKER_MAX_HIGH)] * extra
    history = defects_of(await ctx.attempts(decl)) if extra else []
    for n in range(len(prior), len(plan)):
        origin, effort, max_out = plan[n]
        draft = origin == "calibration_draft"
        if origin == "repair" and critique:
            history = list(dict.fromkeys([*history, *critique]))
        await ctx.activity("R1.worker", decl, f"writing witness attempt {n + 1} for {decl}")
        prompt = worker_prompt(job, None if draft else previous_code, None if draft else critique,
                               history=history if origin == "repair" else None)
        req_kwargs = dict(region="R1.worker", effort=effort, max_output=max_out,
                          instructions=prompts.WORKER_DRAFT if draft else prompts.WORKER,
                          parts=[text_part(prompt)], schema="worker")
        res = await ctx.call_slot(f"{job.step_key}:w{n}", **req_kwargs)
        if not res.ok:
            critique = [f"your reply could not be used ({res.status}: {res.error}); return the JSON object with the whole script"]
            await ctx.write_attempt(decl=decl, attempt=n, origin=origin, effort=effort, outcome="invalid_output",
                                    critique=critique, res=res)
            continue
        out = res.data
        problems = check_script(out.python_source)
        if problems:
            critique = problems
            await ctx.write_attempt(decl=decl, attempt=n, origin=origin, effort=effort, outcome="rejected_static", out=out,
                                    critique=critique, res=res)
            previous_code = out.python_source
            continue
        await ctx.activity("R1.worker", decl, f"running witness attempt {n + 1} in the sandbox")
        sbx = await ctx.sandbox(out.python_source)
        art = next((x for x in sbx.artifacts if x.name == "witness.png"), None)
        if sbx.status != "ok" or art is None:
            tail = (sbx.stderr or "")[-STDERR_TAIL:] or f"status {sbx.status}; no witness.png was written"
            if sbx.violations:
                tail = "sandbox refused: " + "; ".join(f"{v.get('event')}: {v.get('detail')}" for v in sbx.violations[:3]) + "\n" + tail
            critique = [f"the script failed in the sandbox ({sbx.status}):\n{tail}"]
            await ctx.write_attempt(decl=decl, attempt=n, origin=origin, effort=effort, outcome="sandbox_error", out=out,
                                    critique=critique, res=res, stderr=tail, qc=sbx.qc or None)
            previous_code = out.python_source
            continue
        qc = sbx.qc.get("witness.png", {})
        png = await asyncio.to_thread(Path(art.store_path).read_bytes)
        if qc.get("defects"):
            critique = [f"the image failed Eddie's automatic checks: {', '.join(qc['defects'])}"]
            await ctx.write_attempt(decl=decl, attempt=n, origin=origin, effort=effort, outcome="qc_failed", out=out,
                                    critique=critique, res=res, qc=qc, artifact=art, sandbox_job=sbx.job_id,
                                    supersedes=previous_witness, witness_status="rejected",
                                    critic_verdict={"reviewer": "sandbox-qc", "defects": qc["defects"]})
            previous_witness = await ctx.last_witness(decl)
            previous_code = out.python_source
            continue
        await ctx.activity("R2.critic", decl, f"reviewing witness attempt {n + 1}")
        cres = await ctx.call_slot(
            f"{job.step_key}:c{n}", region="R2.critic", effort="medium", max_output=CRITIC_MAX,
            instructions=prompts.CRITIC, parts=[text_part(critic_prompt(job, out, qc)), image_part(fit_for_critic(png))],
            schema="critic", retries=1,
        )
        if not cres.ok:  # no verdict: keep the render pending rather than spend more worker attempts
            await ctx.write_attempt(decl=decl, attempt=n, origin=origin, effort=effort, outcome="critic_unusable", out=out,
                                    critique=[f"R2 gave no usable verdict ({cres.status}: {cres.error})"], res=res, qc=qc,
                                    artifact=art, sandbox_job=sbx.job_id, supersedes=previous_witness,
                                    witness_status="pending", critic=cres)
            return {"verified": None, "pending": await ctx.last_witness(decl), "attempts": n + 1}
        verdict = cres.data
        critique = [*verdict.defects, *(f"fix: {f}" for f in verdict.fixes)] or None
        await ctx.write_attempt(decl=decl, attempt=n, origin=origin, effort=effort, outcome=verdict.verdict, out=out,
                                critique=critique, res=res, qc=qc, artifact=art, sandbox_job=sbx.job_id,
                                supersedes=previous_witness, witness_status=verdict.verdict,
                                critic_verdict=verdict.model_dump(), critic=cres)
        wid = await ctx.last_witness(decl)
        if verdict.verdict == "verified":
            return {"verified": wid, "attempts": n + 1}
        previous_witness = wid
        previous_code = out.python_source
    return {"verified": None, "best": previous_witness, "attempts": len(plan)}
