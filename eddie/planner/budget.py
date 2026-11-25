"""Exact tiered Ember budget planner: `python -m eddie.cli plan-budget`.

Every planned call is costed with the governor's own estimator, region policy and pricing.
Under `--fit worst` (the default), executing the plan cannot cost more than planned even if
every call used its whole reservation. `--fit expected` packs more work using typical ratios.

Tier 1 ($0)  map every module and declaration, and prune dead code (already done by cone build).
Tier 2       R1.triage (minimal): the highest-score live declarations, packed into batches.
Tier 3       per milestone R1.worker -> sandbox ($0) -> R2.critic (plot + spectrogram)
             -> R3.arbiter, plus one R4.consolidator pass per pillar. The roots, the kernel,
             every pillar head and their dominator ancestors come first, so the selected
             milestones always connect to Main.

Text that the pipeline carries from one stage to the next (intuition, code, critique) is
capped at fixed byte budgets. Those caps are recorded in the plan, and tomorrow's pipeline
must truncate to them; that is what keeps every downstream bound exact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from eddie.config import HARD_INPUT_TOKEN_LIMIT, NANO_PER_USD, usd_to_nano
from eddie.llm import pricing
from eddie.llm.estimate import (
    FIXED_OVERHEAD_TOKENS,
    PER_MESSAGE_TOKENS,
    PER_PART_TOKENS,
    image_tokens_upper,
    text_tokens_upper,
)
from eddie.llm.policy import check_region_policy

PLAN_SCHEMA = "eddie-work-plan/1"
TRIAGE_LINE_OVERHEAD = 12  # "[1234567] " + newline framing per declaration in a triage batch
MILESTONE_KINDS = ("theorem", "lemma", "def", "abbrev", "structure", "class", "inductive")


@dataclass(frozen=True)
class Stage:
    region: str
    effort: str
    max_output: int
    template_bytes: int
    summary: str | None = None


@dataclass
class PlanParams:
    target_usd: float = 6.50
    tier2_usd: float = 2.00
    reserve_demo_usd: float = 1.50
    lifetime_cap_usd: float = 8.00
    daily_cap_usd: float = 2.00
    daily_margin: float = 0.05
    concurrency: int = 1
    fit: str = "worst"  # worst | expected
    start_date: str = ""
    triage: Stage = field(default_factory=lambda: Stage("R1.triage", "minimal", 2048, 3000))
    worker: Stage = field(default_factory=lambda: Stage("R1.worker", "high", 4096, 4000))
    critic: Stage = field(default_factory=lambda: Stage("R2.critic", "medium", 1024, 3000))
    arbiter: Stage = field(default_factory=lambda: Stage("R3.arbiter", "xhigh", 8192, 3000, summary="concise"))
    consolidator: Stage = field(default_factory=lambda: Stage("R4.consolidator", "high", 16384, 4000))
    batch_max_tokens: int = 24_000
    min_output_per_decl: int = 40
    neighbors: int = 8
    carry_intuition_bytes: int = 2000
    carry_code_bytes: int = 6000
    carry_critique_bytes: int = 1500
    qc_bytes: int = 800
    plot_px: tuple[int, int] = (1024, 768)
    spectrogram_px: tuple[int, int] = (800, 300)
    retry_reserve_frac: float = 0.15
    r4_chunk_tokens: int = 150_000
    typical_bytes_per_token: float = 3.5
    expected_output_fill: float = 0.5
    max_milestones: int = 5000


@dataclass
class Call:
    stage: Stage
    bound_tokens: int  # governor-identical upper-bound input estimate
    text_tokens: int

    @property
    def reserve_nano(self) -> int:
        return pricing.reserve_nano(self.bound_tokens, self.stage.max_output)

    def expected_nano(self, p: PlanParams) -> int:
        typical_in = FIXED_OVERHEAD_TOKENS + math.ceil(self.text_tokens / p.typical_bytes_per_token) + (self.bound_tokens - FIXED_OVERHEAD_TOKENS - self.text_tokens)
        out = math.ceil(self.stage.max_output * p.expected_output_fill)
        return pricing.cost_nano(max(typical_in, 0), 0, out)

    def to_json(self) -> dict[str, Any]:
        return {
            "region": self.stage.region,
            "effort": self.stage.effort,
            "max_output_tokens": self.stage.max_output,
            "summary": self.stage.summary,
            "est_input_tokens": self.bound_tokens,
            "reserve_usd": self.reserve_nano / NANO_PER_USD,
        }


def bound_tokens(text_tokens: int, *, messages: int = 2, parts: int = 2, image_tokens: int = 0) -> int:
    """Same structure as eddie.llm.estimate.estimate_body (system + user messages)."""
    return FIXED_OVERHEAD_TOKENS + messages * PER_MESSAGE_TOKENS + parts * PER_PART_TOKENS + text_tokens + image_tokens


def _check(stage: Stage) -> None:
    check_region_policy(stage.region, stage.effort, stage.max_output, stage.summary)


def _cost(calls: list[Call], p: PlanParams) -> tuple[int, int]:
    return sum(c.reserve_nano for c in calls), sum(c.expected_nano(p) for c in calls)


def _fit_cost(calls: list[Call], p: PlanParams) -> int:
    worst, expected = _cost(calls, p)
    return worst if p.fit == "worst" else expected


def build_plan(index_path: Path, cone_path: Path, params: PlanParams | None = None) -> dict[str, Any]:
    p = params or PlanParams()
    if p.fit not in ("worst", "expected"):
        raise ValueError("fit must be 'worst' or 'expected'")
    for stage in (p.triage, p.worker, p.critic, p.arbiter, p.consolidator):
        _check(stage)  # never plan a call the governor would refuse
    target = usd_to_nano(_d(p.target_usd))
    tier2_budget = min(usd_to_nano(_d(p.tier2_usd)), target)
    tier3_budget = target - tier2_budget
    retry_reserve = int(tier3_budget * p.retry_reserve_frac)

    con = sqlite3.connect(f"file:{cone_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute(f"ATTACH 'file:{index_path}?mode=ro' AS idx")
    cone_meta = dict(con.execute("SELECT key, value FROM cone_meta").fetchall())
    pillars = {r["id"]: dict(r) for r in con.execute("SELECT id, slug, title, ord, head_decl_id, live FROM pillars ORDER BY ord")}

    # --- Tier 3: milestones (mandatory chain first, then round-robin by pillar score) ------------
    plot_img = image_tokens_upper(*p.plot_px)
    spec_img = image_tokens_upper(*p.spectrogram_px)

    def decl_row(decl_id: int) -> sqlite3.Row:
        return con.execute(
            "SELECT d.id, d.full_name, d.kind, d.signature, d.context, d.docstring, m.pillar_id, m.idom, m.score, m.is_root "
            "FROM idx.decls d JOIN decl_metrics m ON m.decl_id = d.id WHERE d.id = ?",
            (decl_id,),
        ).fetchone()

    def neighbor_tokens(decl_id: int) -> int:
        rows = con.execute(
            "SELECT d.signature FROM edges e JOIN decl_metrics m ON m.decl_id = e.dst JOIN idx.decls d ON d.id = e.dst "
            "WHERE e.src = ? AND m.live = 1 ORDER BY m.score DESC, e.dst LIMIT ?",
            (decl_id, p.neighbors),
        ).fetchall()
        return sum(text_tokens_upper(r[0]) for r in rows)

    def milestone_calls(row: sqlite3.Row) -> list[Call]:
        own = text_tokens_upper(row["signature"]) + text_tokens_upper(row["context"] or "") + text_tokens_upper(row["docstring"] or "")
        worker_text = p.worker.template_bytes + own + neighbor_tokens(row["id"])
        critic_text = p.critic.template_bytes + text_tokens_upper(row["signature"]) + p.qc_bytes + p.carry_intuition_bytes
        arbiter_text = (p.arbiter.template_bytes + own + p.carry_intuition_bytes + p.carry_code_bytes + p.carry_critique_bytes)
        calls = [
            Call(p.worker, bound_tokens(worker_text), worker_text),
            Call(p.critic, bound_tokens(critic_text, parts=3, image_tokens=plot_img), critic_text),
            Call(p.critic, bound_tokens(critic_text, parts=3, image_tokens=spec_img), critic_text),
            Call(p.arbiter, bound_tokens(arbiter_text), arbiter_text),
        ]
        for c in calls:
            if c.bound_tokens > HARD_INPUT_TOKEN_LIMIT:
                raise ValueError(f"{row['full_name']}: a {c.stage.region} call would exceed the 190K input limit")
        return calls

    def r4_calls(n_milestones: int) -> list[Call]:
        if not n_milestones:
            return []
        per_chunk = max(1, (p.r4_chunk_tokens - FIXED_OVERHEAD_TOKENS - p.consolidator.template_bytes) // p.carry_intuition_bytes)
        chunks = math.ceil(n_milestones / per_chunk)
        out = []
        for i in range(chunks):
            n = min(per_chunk, n_milestones - i * per_chunk)
            text = p.consolidator.template_bytes + n * p.carry_intuition_bytes
            out.append(Call(p.consolidator, bound_tokens(text), text))
        return out

    selected: dict[int, dict[str, Any]] = {}
    per_pillar: dict[int, int] = {}
    spent3 = 0

    def r4_total(counts: dict[int, int]) -> int:
        return sum(_fit_cost(r4_calls(n), p) for n in counts.values())

    def try_add(decl_id: int, *, mandatory: bool) -> bool:
        nonlocal spent3
        chain: list[int] = []
        cur = decl_id
        while cur is not None and cur not in selected and cur > 0:
            row = decl_row(cur)
            if row is None:
                break
            chain.append(cur)
            nxt = row["idom"]
            cur = nxt if (nxt is not None and nxt > 0 and nxt < 10**12 and decl_row(nxt) is not None) else None
        chain.reverse()  # ancestors first
        add_cost = 0
        rows = []
        counts = dict(per_pillar)
        for d in chain:
            row = decl_row(d)
            calls = milestone_calls(row)
            rows.append((row, calls))
            add_cost += _fit_cost(calls, p)
            counts[row["pillar_id"]] = counts.get(row["pillar_id"], 0) + 1
        new_total = spent3 + add_cost + r4_total(counts)  # milestones + every pillar's R4 consolidation
        budget = tier3_budget - retry_reserve
        if not mandatory and (new_total > budget or len(selected) + len(rows) > p.max_milestones):
            return False
        for row, calls in rows:
            selected[row["id"]] = {"row": row, "calls": calls, "mandatory": mandatory}
            per_pillar[row["pillar_id"]] = per_pillar.get(row["pillar_id"], 0) + 1
        spent3 += add_cost
        return True

    roots = [r[0] for r in con.execute("SELECT decl_id FROM decl_metrics WHERE is_root = 1 ORDER BY decl_id")]
    kernel = [r[0] for r in con.execute(
        "SELECT m.decl_id FROM decl_metrics m JOIN idx.modules mo ON mo.id = m.module_id "
        "WHERE m.live = 1 AND (mo.name = 'Statements' OR mo.name LIKE 'Supplements.%') ORDER BY m.decl_id")]
    heads = [pl["head_decl_id"] for pl in pillars.values() if pl["head_decl_id"]]
    for d in roots + kernel + heads:
        try_add(d, mandatory=True)
    mandatory_cost = spent3 + r4_total(per_pillar)

    candidates: dict[int, list[int]] = {}
    for pid in pillars:
        candidates[pid] = [r[0] for r in con.execute(
            "SELECT m.decl_id FROM decl_metrics m JOIN idx.decls d ON d.id = m.decl_id "
            f"WHERE m.pillar_id = ? AND m.live = 1 AND m.aux = 0 AND d.kind IN {MILESTONE_KINDS} "
            "ORDER BY m.score DESC, m.decl_id LIMIT ?",
            (pid, p.max_milestones),
        )]
    cursors = {pid: 0 for pid in candidates}
    exhausted: set[int] = set()
    order = sorted(pillars, key=lambda pid: pillars[pid]["ord"])
    while len(exhausted) < len(order) and len(selected) < p.max_milestones:
        progressed = False
        for pid in order:
            if pid in exhausted:
                continue
            lst = candidates[pid]
            while cursors[pid] < len(lst) and lst[cursors[pid]] in selected:
                cursors[pid] += 1
            if cursors[pid] >= len(lst):
                exhausted.add(pid)
                continue
            if try_add(lst[cursors[pid]], mandatory=False):
                progressed = True
                cursors[pid] += 1
            else:
                exhausted.add(pid)  # this pillar's next pick no longer fits
        if not progressed:
            break

    milestone_items = sorted(selected.values(), key=lambda s: (not s["mandatory"], pillars.get(s["row"]["pillar_id"], {}).get("ord", 99), -s["row"]["score"], s["row"]["id"]))
    r4_items = {pid: r4_calls(n) for pid, n in sorted(per_pillar.items())}

    # --- Tier 2: triage batches over the highest-score remaining live declarations --------------
    batches: list[dict[str, Any]] = []
    tier2_spent = 0
    max_per_batch = max(1, p.triage.max_output // p.min_output_per_decl)
    cur_ids: list[int] = []
    cur_text = p.triage.template_bytes

    def flush() -> bool:
        nonlocal cur_ids, cur_text, tier2_spent
        if not cur_ids:
            return True
        call = Call(p.triage, bound_tokens(cur_text), cur_text)
        cost = _fit_cost([call], p)
        if tier2_spent + cost > tier2_budget:
            return False
        batches.append({"decl_ids": cur_ids, "call": call})
        tier2_spent += cost
        cur_ids, cur_text = [], p.triage.template_bytes
        return True

    stop = False
    for decl_id, signature in con.execute(
        "SELECT m.decl_id, d.signature FROM decl_metrics m JOIN idx.decls d ON d.id = m.decl_id "
        f"WHERE m.live = 1 AND d.kind IN {MILESTONE_KINDS} ORDER BY m.score DESC, m.decl_id"
    ):
        if decl_id in selected:
            continue
        add = text_tokens_upper(signature) + TRIAGE_LINE_OVERHEAD
        if cur_ids and (len(cur_ids) >= max_per_batch or bound_tokens(cur_text + add) > p.batch_max_tokens):
            if not flush():
                stop = True
                break
        cur_ids.append(decl_id)
        cur_text += add
    if not stop:
        flush()
    elif cur_ids:  # the partial batch that did not fit is dropped (exactness over greed)
        cur_ids = []

    # --- costs, schedule, guard rails ---------------------------------------------------------------
    t2_calls = [b["call"] for b in batches]
    t3_calls = [c for s in milestone_items for c in s["calls"]]
    t4_calls = [c for calls in r4_items.values() for c in calls]
    w2, e2 = _cost(t2_calls, p)
    w3, e3 = _cost(t3_calls, p)
    w4, e4 = _cost(t4_calls, p)
    all_calls = t2_calls + t3_calls + t4_calls
    max_w = max((pricing.worst_case_w_nano(c.stage.max_output) for c in all_calls), default=0)
    cap = usd_to_nano(_d(p.lifetime_cap_usd))
    demo = usd_to_nano(_d(p.reserve_demo_usd))
    w_tail = p.concurrency * max_w
    warnings = []
    if target + demo > cap - w_tail:
        warnings.append(
            f"target ${p.target_usd:.2f} + demo reserve ${p.reserve_demo_usd:.2f} exceeds the usable lifetime headroom "
            f"${(cap - w_tail) / NANO_PER_USD:.2f} (cap ${p.lifetime_cap_usd:.2f} minus the worst-case tail of "
            f"{p.concurrency} in-flight call(s), ${w_tail / NANO_PER_USD:.3f}); the last demo cents may be refused"
        )
    if mandatory_cost > tier3_budget - retry_reserve:
        warnings.append("the mandatory proof chain alone exceeds the Tier 3 budget")

    start = dt.date.fromisoformat(p.start_date) if p.start_date else dt.date.today() + dt.timedelta(days=1)
    day_cap = int(usd_to_nano(_d(p.daily_cap_usd)) * (1 - p.daily_margin))
    schedule: list[dict[str, Any]] = []
    day, day_spend = start, 0
    work_items: list[dict[str, Any]] = []
    seq = 0

    def place(reserve: int) -> str:
        nonlocal day, day_spend
        if day_spend + reserve > day_cap and day_spend > 0:
            schedule.append({"date": day.isoformat(), "reserve_usd": day_spend / NANO_PER_USD})
            day, day_spend = day + dt.timedelta(days=1), 0
        day_spend += reserve
        return day.isoformat()

    slug = {pid: pl["slug"] for pid, pl in pillars.items()}
    for b in batches:
        seq += 1
        work_items.append({
            "seq": seq, "tier": 2, "kind": "triage_batch", "pillar_slug": None, "decl_full_name": None, "decl_id": None,
            "payload": {"decl_ids": b["decl_ids"], "call": b["call"].to_json()},
            "reserve_nano": b["call"].reserve_nano, "expected_nano": b["call"].expected_nano(p),
            "scheduled_day": place(b["call"].reserve_nano),
        })
    for s in milestone_items:
        seq += 1
        row = s["row"]
        reserve = sum(c.reserve_nano for c in s["calls"])
        work_items.append({
            "seq": seq, "tier": 3, "kind": "milestone", "pillar_slug": slug.get(row["pillar_id"]),
            "decl_full_name": row["full_name"], "decl_id": row["id"],
            "payload": {"mandatory": s["mandatory"], "score": row["score"], "stages": [c.to_json() for c in s["calls"]]},
            "reserve_nano": reserve, "expected_nano": sum(c.expected_nano(p) for c in s["calls"]),
            "scheduled_day": place(reserve),
        })
    for pid, calls in r4_items.items():
        for i, c in enumerate(calls):
            seq += 1
            work_items.append({
                "seq": seq, "tier": 4, "kind": "consolidation", "pillar_slug": slug.get(pid), "decl_full_name": None,
                "decl_id": None, "payload": {"chunk": i, "milestones": per_pillar[pid], "call": c.to_json()},
                "reserve_nano": c.reserve_nano, "expected_nano": c.expected_nano(p), "scheduled_day": place(c.reserve_nano),
            })
    if day_spend:
        schedule.append({"date": day.isoformat(), "reserve_usd": day_spend / NANO_PER_USD})

    coverage = {
        "pillars_total": sum(1 for pl in pillars.values() if pl["live"] > 0),
        "pillars_with_milestones": sum(1 for pid in per_pillar if pillars.get(pid, {}).get("live", 0) > 0),
        "roots_included": all(r in selected for r in roots),
        "dominator_chain_closed": all(
            (s["row"]["idom"] in selected) or s["row"]["is_root"] or not (s["row"]["idom"] and s["row"]["idom"] > 0 and decl_row(s["row"]["idom"]))
            for s in milestone_items
        ),
    }
    totals = {
        "tier2": {"decls": sum(len(b["decl_ids"]) for b in batches), "batches": len(batches),
                  "worst_usd": w2 / NANO_PER_USD, "expected_usd": e2 / NANO_PER_USD},
        "tier3": {"milestones": len(milestone_items), "mandatory": sum(1 for s in milestone_items if s["mandatory"]),
                  "calls": len(t3_calls), "worst_usd": w3 / NANO_PER_USD, "expected_usd": e3 / NANO_PER_USD,
                  "retry_reserve_usd": retry_reserve / NANO_PER_USD},
        "tier4": {"calls": len(t4_calls), "worst_usd": w4 / NANO_PER_USD, "expected_usd": e4 / NANO_PER_USD},
    }
    planned_fit = (w2 + w3 + w4) if p.fit == "worst" else (e2 + e3 + e4)
    params_json = {k: (asdict(v) if isinstance(v, Stage) else v) for k, v in asdict(p).items()}
    for k in ("triage", "worker", "critic", "arbiter", "consolidator"):
        params_json[k] = asdict(getattr(p, k))
    digest_src = json.dumps({"params": params_json, "items": [(w["kind"], w["decl_id"], w.get("payload", {}).get("decl_ids"), w["reserve_nano"]) for w in work_items]}, sort_keys=True, default=str)
    plan = {
        "schema": PLAN_SCHEMA,
        "plan_hash": hashlib.sha256(digest_src.encode()).hexdigest()[:16],
        "generated_at": dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds"),
        "fit": p.fit,
        "parameters": params_json,
        "tier1": {"modules": int(con.execute("SELECT COUNT(*) FROM idx.modules").fetchone()[0]),
                  "decls": int(cone_meta["decls"]), "live": int(cone_meta["live"]), "dead": int(cone_meta["dead"]),
                  "cost_usd": 0.0},
        "totals": totals,
        "planned_usd": planned_fit / NANO_PER_USD,
        "planned_worst_usd": (w2 + w3 + w4) / NANO_PER_USD,
        "planned_expected_usd": (e2 + e3 + e4) / NANO_PER_USD,
        "budget": {"target_usd": p.target_usd, "tier2_usd": tier2_budget / NANO_PER_USD, "tier3_usd": tier3_budget / NANO_PER_USD,
                   "reserve_demo_usd": p.reserve_demo_usd, "lifetime_cap_usd": p.lifetime_cap_usd,
                   "w_tail_usd": w_tail / NANO_PER_USD, "usable_lifetime_usd": (cap - w_tail) / NANO_PER_USD},
        "coverage": coverage,
        "schedule": schedule,
        "warnings": warnings,
        "carry_caps_bytes": {"intuition": p.carry_intuition_bytes, "code": p.carry_code_bytes, "critique": p.carry_critique_bytes},
        "work_items": work_items,
    }
    con.close()
    return plan


def _d(value: float):
    from decimal import Decimal

    return Decimal(str(value))
