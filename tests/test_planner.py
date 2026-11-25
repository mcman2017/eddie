"""Budget planner: governor-exact costs, budget fit, coverage, determinism, schedule, warnings."""

from __future__ import annotations

import gzip
import sqlite3
from dataclasses import replace

import pytest

from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone
from eddie.ingest.synthetic import write_sample_tree
from eddie.llm import pricing
from eddie.llm.body import build_body
from eddie.llm.errors import ForbiddenParameterError
from eddie.llm.estimate import estimate_body
from eddie.llm.types import EddieRequest
from eddie.planner.budget import PlanParams, Stage, build_plan

pytestmark = pytest.mark.slow
NANO = 1_000_000_000


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("plan")
    write_sample_tree(root / "tree", scale=6)
    index = root / "index.sqlite"
    build_index(root / "tree", index, workers=1, log=lambda *_: None)
    cone = root / "cone.sqlite"
    spine = root / "spine.tsv.gz"
    con = sqlite3.connect(index)
    names = [r[0] for r in con.execute("SELECT full_name FROM decls WHERE full_name IS NOT NULL")]
    con.close()
    with gzip.open(spine, "wt") as fh:  # everything outside BuildAid is live in this toy cone
        for n in names:
            if not n.startswith("Edge.") and n not in ("rootLevel",):
                fh.write(f"{n}\tthm\n")
    build_cone(index, cone, spine_path=spine, workers=1, log=lambda *_: None)
    return index, cone


def small(**kw) -> PlanParams:
    base = PlanParams(target_usd=0.08, tier2_usd=0.01, reserve_demo_usd=0.0, start_date="2026-11-25")
    return replace(base, **kw)


def test_budget_fit_and_coverage(built):
    index, cone = built
    plan = build_plan(index, cone, small(target_usd=0.60, tier2_usd=0.01))
    items = plan["work_items"]
    t2 = sum(i["reserve_nano"] for i in items if i["tier"] == 2)
    t34 = sum(i["reserve_nano"] for i in items if i["tier"] in (3, 4))
    assert 0 < t2 <= 0.01 * NANO
    assert t34 <= (0.59 * NANO) * (1 - 0.15) + 1  # milestones + R4 never touch the retry reserve
    assert not plan["warnings"]
    cov = plan["coverage"]
    assert cov["roots_included"] and cov["dominator_chain_closed"]
    assert plan["totals"]["tier3"]["mandatory"] >= 2  # both roots at least
    assert plan["totals"]["tier3"]["milestones"] > plan["totals"]["tier3"]["mandatory"]


def test_mandatory_chain_over_budget_is_flagged(built):
    index, cone = built
    plan = build_plan(index, cone, small(target_usd=0.03, tier2_usd=0.01))
    assert any("mandatory proof chain" in w for w in plan["warnings"])


def test_every_stage_cost_is_the_governor_reservation(built):
    index, cone = built
    plan = build_plan(index, cone, small())
    for item in plan["work_items"]:
        stages = item["payload"].get("stages") or [item["payload"]["call"]]
        recomputed = sum(pricing.reserve_nano(s["est_input_tokens"], s["max_output_tokens"]) for s in stages)
        assert recomputed == item["reserve_nano"]


def test_planned_triage_bound_dominates_the_real_request_estimate(built):
    """Build the request tomorrow's pipeline would send for a batch: the governor's estimate
    must never exceed what the planner reserved."""
    index, cone = built
    plan = build_plan(index, cone, small())
    batch = next(i for i in plan["work_items"] if i["kind"] == "triage_batch")
    con = sqlite3.connect(index)
    lines = [
        f"[{decl_id}] " + con.execute("SELECT signature FROM decls WHERE id = ?", (decl_id,)).fetchone()[0]
        for decl_id in batch["payload"]["decl_ids"]
    ]
    con.close()
    template = "x" * PlanParams().triage.template_bytes
    req = EddieRequest.simple("\n".join(lines), brain_region="R1.triage", effort="minimal",
                              max_output_tokens=2048, instructions=template)
    real = estimate_body(build_body(req)).input_tokens
    assert real <= batch["payload"]["call"]["est_input_tokens"]


def test_plan_is_deterministic(built):
    index, cone = built
    a = build_plan(index, cone, small())
    b = build_plan(index, cone, small())
    assert a["plan_hash"] == b["plan_hash"]
    assert [i["decl_id"] for i in a["work_items"]] == [i["decl_id"] for i in b["work_items"]]


def test_schedule_respects_the_daily_cap(built):
    index, cone = built
    plan = build_plan(index, cone, small(daily_cap_usd=0.05, target_usd=0.60))
    for day in plan["schedule"]:
        assert day["reserve_usd"] <= 0.05 * 0.95 + 1e-9
    assert len(plan["schedule"]) >= 3


def test_w_tail_warning(built):
    index, cone = built
    plan = build_plan(index, cone, small(target_usd=7.0, tier2_usd=2.0, reserve_demo_usd=1.0, concurrency=2))
    assert any("worst-case tail" in w for w in plan["warnings"])


def test_planner_refuses_stages_the_governor_would_refuse(built):
    index, cone = built
    bad = small(triage=Stage("R1.triage", "high", 2048, 3000))
    with pytest.raises(ForbiddenParameterError):
        build_plan(index, cone, bad)
