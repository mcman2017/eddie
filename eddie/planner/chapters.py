"""The proof atlas: one chapter per pillar, in flow order, described from the cone alone ($0).

`chapter_facts` returns structure that is true whatever a model later writes about the
chapter: its place in the four acts, what it builds on and what builds on it (net reference
flow, provider -> consumer), and its bottleneck theorems, the declarations that dominate the
most of the live proof. `stub_markdown` turns those facts into the placeholder text that
`seed-demo` stores with source='seed' until the R4 consolidator writes the real chapter.

Only sqlite3 is used here, so the read-only API can import this module.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

STUB_BADGE = "$0 Structural Stub — Ready for R4 Muse Ember"
TIER_LABEL = {"foundations": "Foundations", "machinery": "Machinery", "bottleneck": "Bottleneck", "main": "Summit"}
ACT = {"foundations": "Act I", "machinery": "Act II", "bottleneck": "Act III", "main": "Act IV"}
SIGNATURE_CHARS = 600
BOTTLENECK_KINDS = ("theorem", "lemma")


def chapter_rows(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every pillar in chapter order. `con` is the cone database with the index attached as idx."""
    rows = []
    for r in con.execute(
        "SELECT p.id, p.slug, p.title, p.subtitle, p.category, p.tier, p.flow_ord, p.modules, p.decls, p.live, p.dead, "
        "p.ref_in, p.ref_out, p.max_dom_share, p.median_depth, p.timeline, p.head_decl_id, d.full_name AS head_name "
        "FROM pillars p LEFT JOIN idx.decls d ON d.id = p.head_decl_id ORDER BY p.flow_ord"
    ):
        both = r["ref_in"] + r["ref_out"]
        rows.append({
            "pillar_id": r["id"], "slug": r["slug"], "title": r["title"], "category": r["category"], "tier": r["tier"],
            "tier_label": TIER_LABEL.get(r["tier"], r["tier"]), "chapter": r["flow_ord"] + 1, "modules": r["modules"],
            "decls": r["decls"], "live": r["live"], "dead": r["dead"], "ref_in": r["ref_in"], "ref_out": r["ref_out"],
            "net": round((r["ref_in"] - r["ref_out"]) / both, 4) if both else 0.0,
            "max_dom_share": r["max_dom_share"], "median_depth": r["median_depth"],
            "remark_steps": json.loads(r["timeline"]), "head": {"id": r["head_decl_id"], "full_name": r["head_name"]},
        })
    for i, row in enumerate(rows):
        row["chapters"] = len(rows)
        row["prev"] = rows[i - 1]["slug"] if i > 0 else None
        row["next"] = rows[i + 1]["slug"] if i + 1 < len(rows) else None
    return rows


def net_flows(con: sqlite3.Connection) -> list[dict[str, int]]:
    """Net reference flow between pillars as provider -> consumer, heaviest first.

    `pillar_edges(source, target, weight)` counts references from source to target, so the
    source builds on the target and the proof flows the other way.
    """
    weight: dict[tuple[int, int], int] = {}
    for s, t, w in con.execute("SELECT source, target, weight FROM pillar_edges ORDER BY source, target"):
        weight[(s, t)] = w
    out = []
    for (a, b), ab in weight.items():
        ba = weight.get((b, a), 0)
        if ab > ba:  # a builds on b more than the reverse: b provides, a consumes
            out.append({"provider": b, "consumer": a, "net": ab - ba, "forward": ab, "backward": ba})
    out.sort(key=lambda f: (-f["net"], f["provider"], f["consumer"]))
    return out


def _theorems(con: sqlite3.Connection, where: str, params: tuple, live_total: int) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT d.id, d.kind, d.full_name, d.signature, m.dom_mass, m.score, m.depth FROM decl_metrics m "
        f"JOIN idx.decls d ON d.id = m.decl_id WHERE m.live = 1 AND d.full_name IS NOT NULL AND {where}", params,
    ).fetchall()
    return [{
        "id": r["id"], "kind": r["kind"], "full_name": r["full_name"], "signature": (r["signature"] or "")[:SIGNATURE_CHARS],
        "dom_mass": r["dom_mass"], "dom_share": round(r["dom_mass"] / live_total, 6) if live_total else 0.0,
        "score": r["score"], "depth": r["depth"],
    } for r in rows]


def bottlenecks(con: sqlite3.Connection, pillar_id: int, live_total: int, limit: int = 5) -> list[dict[str, Any]]:
    """The chapter's theorems that dominate the most: every path from Main to `dom_mass - 1`
    other live declarations passes through each of them. Short wrapper theorems are kept on
    purpose: a one-line theorem that gates most of the proof is exactly a bottleneck. Main's
    own final theorems are listed separately (`final_theorems`): they are the proof's ends."""
    marks = ", ".join("?" for _ in BOTTLENECK_KINDS)
    return _theorems(
        con, f"m.pillar_id = ? AND m.is_root = 0 AND m.dom_mass > 1 AND d.kind IN ({marks}) "
             "ORDER BY m.dom_mass DESC, m.score DESC, m.decl_id LIMIT ?",
        (pillar_id, *BOTTLENECK_KINDS, limit), live_total)


def final_theorems(con: sqlite3.Connection, pillar_id: int, live_total: int) -> list[dict[str, Any]]:
    """The root theorems that live in this chapter, in source order (empty outside the last chapter)."""
    return _theorems(con, "m.pillar_id = ? AND m.is_root = 1 ORDER BY d.id LIMIT 12", (pillar_id,), live_total)


def chapter_facts(con: sqlite3.Connection, *, limit: int = 5, neighbours: int = 4) -> list[dict[str, Any]]:
    """Structural facts for every chapter, in chapter order."""
    rows = chapter_rows(con)
    by_id = {r["pillar_id"]: r for r in rows}
    live_total = int(con.execute("SELECT value FROM cone_meta WHERE key = 'live'").fetchone()[0])
    flows = net_flows(con)

    def side(row: dict[str, Any], mine: str, other: str) -> list[dict[str, Any]]:
        picked = [f for f in flows if f[mine] == row["pillar_id"] and f[other] in by_id][:neighbours]
        return [{"slug": by_id[f[other]]["slug"], "title": by_id[f[other]]["title"], "chapter": by_id[f[other]]["chapter"],
                 "net": f["net"], "forward": f["forward"], "backward": f["backward"]} for f in picked]

    for row in rows:
        row["live_total"] = live_total
        row["builds_on"] = side(row, "consumer", "provider")
        row["feeds"] = side(row, "provider", "consumer")
        row["bottlenecks"] = bottlenecks(con, row["pillar_id"], live_total, limit)
        row["final_theorems"] = final_theorems(con, row["pillar_id"], live_total)
    return rows


def short_title(title: str) -> str:
    return title.split(" · ")[-1]


def stub_markdown(facts: dict[str, Any]) -> str:
    """Placeholder chapter text built only from measured structure. Its first line is the badge."""
    f = facts
    name = short_title(f["title"])
    lines = [STUB_BADGE, ""]
    dead = f", and {f['dead']:,} more that the proof never uses" if f["dead"] else ""
    lines.append(
        f"**Chapter {f['chapter']} of {f['chapters']}, in {ACT.get(f['tier'], 'the proof')} ({f['tier_label'].lower()}).** {name} holds {f['live']:,} live "
        f"declarations in {f['modules']:,} modules{dead}."
    )
    lines.append("")
    if f["tier"] == "main":
        lines.append("This is where the proof ends: the final theorems in `Main`, the statements they prove and the kernel "
                     "they delegate to. Everything else in the atlas flows into this chapter.")
    elif f["ref_in"] + f["ref_out"]:
        role = "a net provider" if f["net"] > 0 else "a net consumer"
        lines.append(
            f"It is {role}: other chapters build on it {f['ref_in']:,} times and it builds on them {f['ref_out']:,} times."
        )
    for label, key in (("Builds on", "builds_on"), ("Feeds", "feeds")):
        if f[key]:
            lines += ["", f"**{label}:** " + "; ".join(
                f"{short_title(n['title'])} (chapter {n['chapter']}, {n['net']:,} net references)" for n in f[key]) + "."]
    if f["final_theorems"]:
        lines += ["", "**Final theorems.** What the whole archive proves:", ""]
        lines += [f"- `{t['full_name']}`" for t in f["final_theorems"]]
    if f["bottlenecks"]:
        lines += ["", "**Chokepoints.** Every route from the final theorem to the stated number of declarations runs through each:", ""]
        for b in f["bottlenecks"]:
            lines.append(f"- `{b['full_name']}` gates {max(b['dom_mass'] - 1, 0):,} declarations "
                         f"({100 * b['dom_share']:.1f}% of the live proof)")
    if f["remark_steps"]:
        lines += ["", "**Authors' summary PDF (keyword match):** " + "; ".join(
            f"step {s['number']}, {s['phrase']}" for s in f["remark_steps"]) + "."]
    lines += ["", "The R4 consolidator will replace this stub with a synthesis of the chapter's verified milestones."]
    return "\n".join(lines)


def chapter_stubs(cone_path, index_path) -> list[dict[str, Any]]:
    """(slug, title, markdown) for every chapter, read from the cone and the index."""
    con = sqlite3.connect(f"file:{cone_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        con.execute("ATTACH ? AS idx", (f"file:{index_path}?mode=ro",))
        return [{"slug": f["slug"], "title": f["title"], "chapter": f["chapter"], "markdown": stub_markdown(f)}
                for f in chapter_facts(con)]
    finally:
        con.close()
