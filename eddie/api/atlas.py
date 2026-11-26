"""Read-only atlas API: the chapters of the proof and the precomputed 3D landscape.

Chapter structure comes from the cone (always true, $0); chapter text comes from the
`chapters` table, where `seed-demo` stores stubs and the R4 consolidator will later store
its syntheses. A stub is always returned with its badge so it can never pass for model
output. The landscape is served exactly as `build-landscape` wrote it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from eddie.planner import chapters as ch

if TYPE_CHECKING:
    from eddie.api.explorer import ExplorerData

LANDSCAPE_HINT = "landscape not built: run `uv run python -m eddie.cli build-landscape`"
LIST_KEYS = ("pillar_id", "slug", "title", "category", "tier", "tier_label", "chapter", "chapters", "modules", "decls", "live",
             "dead", "ref_in", "ref_out", "net", "max_dom_share", "remark_steps", "prev", "next")


def witness_json(w: sqlite3.Row) -> dict[str, Any]:
    """One witness row in the shape both the inspector and the chapter guide read."""
    out = dict(w)
    out["url"] = f"/api/artifacts/{w['artifact_sha256']}.{w['ext']}"
    out["defects"] = json.loads(w["defects"] or "[]")
    out["qc"] = json.loads(w["qc"]) if w["qc"] else None
    out["critic_verdict"] = json.loads(w["critic_verdict"]) if w["critic_verdict"] else None
    return out


def register(router: APIRouter, data: ExplorerData) -> None:
    def facts(con: sqlite3.Connection) -> list[dict[str, Any]]:
        return data.cached("chapter_facts", data._mtime(data.cone), lambda: ch.chapter_facts(con))

    def texts(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        if not data.has_artifacts():
            return {}
        return {r["pillar_slug"]: dict(r) for r in con.execute("SELECT * FROM art.chapters")}

    def badge(text: dict[str, Any] | None) -> str | None:
        return ch.STUB_BADGE if text and text["source"] == "seed" else None

    @router.get("/chapters")
    def chapters() -> dict[str, Any]:
        with data.cone_con() as con:
            rows = facts(con)
            written = texts(con)
            node = {r[0]: r[1] for r in con.execute("SELECT pillar_id, id FROM tree_nodes WHERE kind = 'pillar'")}
            meta = dict(con.execute("SELECT key, value FROM cone_meta WHERE key IN ('flow_forward_pct', 'flow_method')").fetchall())
            witnessed: dict[int, int] = {}
            if data.has_artifacts():
                witnessed = dict(con.execute(
                    "SELECT m.pillar_id, COUNT(*) FROM art.witnesses w JOIN idx.decls d ON d.full_name = w.decl_full_name "
                    "JOIN decl_metrics m ON m.decl_id = d.id WHERE w.status = 'verified' GROUP BY m.pillar_id").fetchall())
        out = []
        for f in rows:
            text = written.get(f["slug"])
            out.append({**{k: f[k] for k in LIST_KEYS}, "source": text["source"] if text else None, "badge": badge(text),
                        "node_id": node.get(f["pillar_id"]), "verified_witnesses": witnessed.get(f["pillar_id"], 0)})
        return {"chapters": out, "stub_badge": ch.STUB_BADGE,
                "flow": {"forward_pct": float(meta.get("flow_forward_pct") or 0.0), "method": meta.get("flow_method")}}

    @router.get("/chapters/{slug}")
    def chapter(slug: str) -> dict[str, Any]:
        with data.cone_con() as con:
            f = next((x for x in facts(con) if x["slug"] == slug), None)
            if f is None:
                raise HTTPException(404, "no such chapter")
            out: dict[str, Any] = dict(f)
            text = texts(con).get(slug)
            out["source"] = text["source"] if text else None
            out["badge"] = badge(text)
            out["synthesis_md"] = text["synthesis_md"] if text else None
            out["headline"] = text.get("headline") if text else None
            out["receipt"] = None
            out["pipeline"] = None
            row = con.execute("SELECT id FROM tree_nodes WHERE kind = 'pillar' AND pillar_id = ?", (f["pillar_id"],)).fetchone()
            out["node_id"] = row[0] if row else None
            statuses = data.status_by_decl(con)
            for key in ("bottlenecks", "final_theorems"):
                out[key] = [{**b, "status": statuses.get(b["id"], "live")} for b in f[key]]
            out["witnesses"], out["reviews"], out["planned"], out["receipts"] = [], [], None, {}
            if data.has_artifacts():
                out["witnesses"] = [witness_json(w) for w in con.execute(
                    "SELECT w.*, d.id AS decl_id FROM art.witnesses w JOIN idx.decls d ON d.full_name = w.decl_full_name "
                    "JOIN decl_metrics m ON m.decl_id = d.id WHERE m.pillar_id = ? ORDER BY w.id", (f["pillar_id"],))]
                ids = [w["id"] for w in out["witnesses"]]
                if ids:
                    marks = ", ".join("?" for _ in ids)
                    from eddie.api.explorer import review_json

                    out["reviews"] = [review_json(v) for v in con.execute(
                        f"SELECT * FROM art.reviews WHERE witness_id IN ({marks}) ORDER BY id", ids)]
                from eddie.api.explorer import has_table, receipts

                calls = [text.get("call_id") if text else None] + [w.get("call_id") for w in out["witnesses"]]
                calls += [v.get("call_id") for v in out["reviews"]]
                found = receipts(con, calls)
                out["receipts"] = found
                if text and text.get("call_id"):
                    out["receipt"] = found.get(text["call_id"])
                if has_table(con, "art", "pipeline_steps"):
                    steps = con.execute(
                        "SELECT kind, status, error FROM art.pipeline_steps WHERE pillar_slug = ? AND kind <> 'call'", (slug,)
                    ).fetchall()
                    if steps:
                        counts: dict[str, dict[str, int]] = {}
                        for st in steps:
                            counts.setdefault(st["kind"], {}).setdefault(st["status"], 0)
                            counts[st["kind"]][st["status"]] += 1
                        out["pipeline"] = {"steps": counts,
                                           "errors": [st["error"] for st in steps if st["status"] == "failed" and st["error"]][:5]}
                plan = {r[0]: (r[1], r[2]) for r in con.execute(
                    "SELECT tier, COUNT(*), MIN(scheduled_day) FROM art.work_items WHERE pillar_slug = ? GROUP BY tier", (slug,))}
                if plan:
                    out["planned"] = {"milestones": plan.get(3, (0, None))[0], "first_day": plan.get(3, (0, None))[1],
                                      "consolidation_day": plan.get(4, (0, None))[1]}
            return out

    def landscape_payload() -> bytes:
        path = data.landscape / "landscape.json"
        if not path.is_file():
            raise HTTPException(503, LANDSCAPE_HINT)

        def build() -> bytes:
            raw = json.loads(path.read_text())
            raw["stale"] = None
            if data.cone.exists():
                con = sqlite3.connect(f"file:{data.cone}?mode=ro", uri=True)
                try:
                    row = con.execute("SELECT value FROM cone_meta WHERE key = 'built_at'").fetchone()
                finally:
                    con.close()
                raw["stale"] = bool(row) and row[0] != raw.get("build", {}).get("cone_built_at")
            return json.dumps(raw, separators=(",", ":")).encode()

        return data.cached("landscape", data._mtime(path, data.cone), build)

    @router.get("/landscape")
    def landscape() -> Response:
        return Response(landscape_payload(), media_type="application/json", headers={"Cache-Control": "no-cache"})

    @router.get("/landscape/fields")
    def landscape_fields() -> FileResponse:
        path = data.landscape / "fields.f32"
        if not path.is_file():
            raise HTTPException(503, LANDSCAPE_HINT)
        return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": "no-cache"})
