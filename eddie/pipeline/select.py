"""$0, deterministic choice of what each region sees.

Per pillar (chapter): the declarations to triage, the gate theorem R3 audits, and the
chokepoints from which R1.worker's flagship witness is chosen once triage has scored how
visualizable each one is. Everything comes from the cone and the index; nothing calls a model.

Triage ranks by dominated mass first: a one-line wrapper theorem that gates most of the proof
(`allChi`) is exactly what a reader needs explained, so short "aux" lemmas are not excluded.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from eddie.planner.budget import MILESTONE_KINDS
from eddie.planner.chapters import bottlenecks

PER_PILLAR = 40
CHOKEPOINTS = 5


@dataclass(frozen=True)
class DeclRef:
    id: int
    full_name: str
    kind: str
    pillar_id: int | None
    dom_mass: int
    score: float
    is_root: bool


@dataclass
class PillarPlan:
    pillar_id: int
    slug: str
    title: str
    chapter: int
    tier: str
    triage: list[DeclRef] = field(default_factory=list)
    gate: DeclRef | None = None
    chokepoints: list[DeclRef] = field(default_factory=list)


@dataclass
class Selection:
    pillars: list[PillarPlan]
    main_theorem: DeclRef | None
    main_slug: str | None

    def pillar(self, slug: str) -> PillarPlan | None:
        return next((p for p in self.pillars if p.slug == slug), None)

    @property
    def triage_count(self) -> int:
        return sum(len(p.triage) for p in self.pillars)


def open_cone(cone_path: Path, index_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{cone_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH ? AS idx", (f"file:{index_path}?mode=ro",))
    return con


def _ref(r: sqlite3.Row) -> DeclRef:
    return DeclRef(int(r["id"]), r["full_name"], r["kind"], r["pillar_id"], int(r["dom_mass"] or 0),
                   float(r["score"] or 0.0), bool(r["is_root"]))


_DECL_SQL = (
    "SELECT d.id, d.full_name, d.kind, m.pillar_id, m.dom_mass, m.score, m.is_root FROM decl_metrics m "
    "JOIN idx.decls d ON d.id = m.decl_id "
)


def mandatory_ids(plan_path: Path | None) -> set[int]:
    """The roots, the kernel, every pillar head and their dominator ancestors (work_plan.json)."""
    if plan_path is None or not plan_path.exists():
        return set()
    plan = json.loads(plan_path.read_text())
    return {int(w["decl_id"]) for w in plan.get("work_items", [])
            if w.get("kind") == "milestone" and (w.get("payload") or {}).get("mandatory") and w.get("decl_id")}


def select(cone_path: Path, index_path: Path, *, per_pillar: int = PER_PILLAR, plan_path: Path | None = None) -> Selection:
    con = open_cone(cone_path, index_path)
    try:
        live_total = int(con.execute("SELECT value FROM cone_meta WHERE key = 'live'").fetchone()[0])
        must = mandatory_ids(plan_path)
        marks = ", ".join("?" for _ in MILESTONE_KINDS)
        pillars: list[PillarPlan] = []
        for p in con.execute("SELECT id, slug, title, tier, flow_ord FROM pillars ORDER BY flow_ord").fetchall():
            plan = PillarPlan(int(p["id"]), p["slug"], p["title"], int(p["flow_ord"]) + 1, p["tier"])
            seen: set[int] = set()

            def take(rows: list[sqlite3.Row], plan: PillarPlan = plan, seen: set[int] = seen) -> None:
                for r in rows:
                    if r["id"] not in seen and r["full_name"]:
                        seen.add(r["id"])
                        plan.triage.append(_ref(r))

            if p["tier"] == "main":  # the summit is small: every live, named declaration in it
                take(con.execute(_DECL_SQL + "WHERE m.pillar_id = ? AND m.live = 1 AND d.full_name IS NOT NULL "
                                 "ORDER BY m.is_root DESC, m.dom_mass DESC, d.id", (p["id"],)).fetchall())
            else:
                if must:
                    ids = ", ".join(str(int(i)) for i in sorted(must))
                    take(con.execute(_DECL_SQL + f"WHERE m.pillar_id = ? AND m.live = 1 AND d.id IN ({ids}) "
                                     "ORDER BY m.dom_mass DESC, d.id", (p["id"],)).fetchall())
                take(con.execute(
                    _DECL_SQL + f"WHERE m.pillar_id = ? AND m.live = 1 AND d.full_name IS NOT NULL AND d.kind IN ({marks}) "
                    "ORDER BY m.dom_mass DESC, m.score DESC, d.id LIMIT ?", (p["id"], *MILESTONE_KINDS, per_pillar),
                ).fetchall())
            chokes = bottlenecks(con, int(p["id"]), live_total, CHOKEPOINTS)
            plan.chokepoints = [
                DeclRef(int(b["id"]), b["full_name"], b["kind"], int(p["id"]), int(b["dom_mass"]), float(b["score"]), False)
                for b in chokes
            ]
            if plan.chokepoints:
                plan.gate = plan.chokepoints[0]
            else:  # a pillar with no theorem gating anything: its most central theorem stands in
                plan.gate = next((d for d in plan.triage if d.kind in ("theorem", "lemma") and not d.is_root), None)
            for c in plan.chokepoints:  # a chokepoint is always triaged, so the witness pick has its score
                if c.id not in seen:
                    seen.add(c.id)
                    plan.triage.append(c)
            pillars.append(plan)

        roots = con.execute(_DECL_SQL + "WHERE m.is_root = 1 AND d.full_name IS NOT NULL ORDER BY d.id").fetchall()
        main = next((r for r in roots if "RiemannHypothesis" in (con.execute(
            "SELECT signature FROM idx.decls WHERE id = ?", (r["id"],)).fetchone()[0] or "")), roots[0] if roots else None)
        main_slug = next((p.slug for p in pillars if p.tier == "main"), None)
        return Selection(pillars, _ref(main) if main is not None else None, main_slug)
    finally:
        con.close()
