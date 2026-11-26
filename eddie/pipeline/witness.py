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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eddie.pipeline import prompts
from eddie.pipeline.calls import image_part, text_part
from eddie.pipeline.context import CARRY_CODE, CARRY_CRITIQUE, Card, clip, render
from eddie.pipeline.scriptcheck import check_script

if TYPE_CHECKING:
    from eddie.pipeline.orchestrator import RunContext

WORKER_MAX = 12_288
WORKER_MAX_HIGH = 16_384
CRITIC_MAX = 2_048
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


def worker_prompt(job: WitnessJob, previous_code: str | None, critique: list[str] | None) -> str:
    parts = ["Declaration to illustrate:", render(job.target, label="target")]
    if job.gloss:
        parts += ["", f"First reader's plain-English gloss: {job.gloss}"]
    if job.neighbours:
        parts += ["", "It builds on (for context only):"]
        parts += [render(n, with_context=False) for n in job.neighbours]
    if previous_code is not None:
        parts += ["", prompts.REPAIR, "Previous script:", "```python", clip(previous_code, CARRY_CODE), "```",
                  "What was wrong:", clip("\n".join(f"- {c}" for c in (critique or ["(no detail)"])), CARRY_CRITIQUE)]
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


async def run_witness(ctx: RunContext, job: WitnessJob) -> dict[str, Any]:
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
    plan = schedule(job.calibration_draft)
    for n in range(len(prior), len(plan)):
        origin, effort, max_out = plan[n]
        draft = origin == "calibration_draft"
        await ctx.activity("R1.worker", decl, f"writing witness attempt {n + 1} for {decl}")
        req_kwargs = dict(region="R1.worker", effort=effort, max_output=max_out,
                          instructions=prompts.WORKER_DRAFT if draft else prompts.WORKER,
                          parts=[text_part(worker_prompt(job, None if draft else previous_code, None if draft else critique))],
                          schema="worker")
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
