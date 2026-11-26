"""Read-only explorer API: overview, the zoomable proof map, the theorem inspector, artifacts.

Every database is opened `mode=ro`. Nothing here can trigger a model call or write state.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from eddie import paths
from eddie.api.atlas import register as register_atlas
from eddie.api.atlas import witness_json
from eddie.api.media import register as register_media
from eddie.planner import chapters as ch

CONE_VERSION = "2"
ARTIFACT_NAME = re.compile(r"^[0-9a-f]{64}\.(png|wav|json|txt|csv|gif)$")
MEDIA = {"png": "image/png", "gif": "image/gif", "wav": "audio/wav", "json": "application/json",
         "txt": "text/plain; charset=utf-8", "csv": "text/csv; charset=utf-8"}


def _env_path(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw) if raw else default


class ExplorerData:
    def __init__(self) -> None:
        data = paths.data_dir()
        self.index = _env_path("EDDIE_INDEX_PATH", data / "index" / "eddie_index.sqlite")
        self.cone = _env_path("EDDIE_CONE_PATH", data / "index" / "eddie_cone.sqlite")
        self.artifacts_db = _env_path("EDDIE_ARTIFACTS_DB", data / "state" / "eddie_artifacts.sqlite")
        self.artifact_dir = _env_path("EDDIE_ARTIFACT_DIR", data / "artifacts")
        self.plan = _env_path("EDDIE_WORK_PLAN", data / "index" / "work_plan.json")
        self.landscape = _env_path("EDDIE_LANDSCAPE_DIR", data / "index" / "landscape")
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[Any, Any]] = {}

    # --- connections -------------------------------------------------------------------------

    @contextmanager
    def cone_con(self) -> Iterator[sqlite3.Connection]:
        if not self.cone.exists() or not self.index.exists():
            raise HTTPException(503, "cone not built: run `uv run python -m eddie.cli cone build`")
        con = sqlite3.connect(f"file:{self.cone}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            version = con.execute("SELECT value FROM cone_meta WHERE key = 'cone_version'").fetchone()
            if version is None or version[0] != CONE_VERSION:
                raise HTTPException(503, "cone was built by an older version: run `uv run python -m eddie.cli cone build` again")
            con.execute("ATTACH ? AS idx", (f"file:{self.index}?mode=ro",))
            if self.artifacts_db.exists():
                con.execute("ATTACH ? AS art", (f"file:{self.artifacts_db}?mode=ro",))
            yield con
        finally:
            con.close()

    def has_artifacts(self) -> bool:
        return self.artifacts_db.exists()

    def _mtime(self, *files: Path) -> tuple:
        """Cache key: each file's real path and mtime. In the pod the data sits behind an atomically
        swapped `current` symlink; keying on the resolved path makes a swap visible on the next
        request even when a file's mtime is unchanged (tar keeps mtimes)."""
        out = []
        for f in files:
            real = os.path.realpath(f)
            out.append((real, os.stat(real).st_mtime if os.path.exists(real) else None))
        return tuple(out)

    def source_available(self, con: sqlite3.Connection) -> bool:
        """Whether proof bodies can be read here (only on the host that has the archive)."""
        def build() -> bool:
            row = con.execute("SELECT value FROM idx.index_meta WHERE key = 'source_root'").fetchone()
            return bool(row) and Path(row[0]).is_dir()

        return self.cached("source_available", self._mtime(self.index), build)

    def cached(self, key: str, deps: tuple, build):
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] == deps:
                return hit[1]
        value = build()
        with self._lock:
            self._cache[key] = (deps, value)
        return value

    # --- status overlay -------------------------------------------------------------------------

    def status_by_decl(self, con: sqlite3.Connection) -> dict[int, str]:
        """decl_id -> 'verified' | 'triaged' for declarations the pipeline has touched."""
        if not self.has_artifacts():
            return {}

        def build() -> dict[int, str]:
            rows = con.execute(
                "SELECT d.id, s.status FROM art.v_decl_status s JOIN idx.decls d ON d.full_name = s.decl_full_name"
            ).fetchall()
            return {r[0]: r[1] for r in rows if r[1] in ("verified", "triaged")}

        return self.cached("status", self._mtime(self.artifacts_db, self.cone), build)

    def node_status_counts(self, con: sqlite3.Connection) -> dict[int, dict[str, int]]:
        """tree node id -> {'verified': n, 'triaged': n} aggregated over its subtree."""
        statuses = self.status_by_decl(con)
        if not statuses:
            return {}

        def build() -> dict[int, dict[str, int]]:
            parent = {r[0]: r[1] for r in con.execute("SELECT id, parent_id FROM tree_nodes")}
            module_node = {r[0]: r[1] for r in con.execute("SELECT module_id, id FROM tree_nodes WHERE kind = 'module'")}
            counts: dict[int, dict[str, int]] = {}
            ids = ",".join(str(int(d)) for d in statuses)
            for decl_id, module_id in con.execute(f"SELECT id, module_id FROM idx.decls WHERE id IN ({ids})"):
                node = module_node.get(module_id)
                while node is not None:
                    bucket = counts.setdefault(node, {"verified": 0, "triaged": 0})
                    bucket[statuses[decl_id]] += 1
                    node = parent.get(node)
            return counts

        return self.cached("node_status", self._mtime(self.artifacts_db, self.cone), build)


def has_table(con: sqlite3.Connection, schema: str, table: str) -> bool:
    return con.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def receipts(con: sqlite3.Connection, call_ids: list[str | None]) -> dict[str, dict[str, Any]]:
    """call_id -> the display receipt of the model call that produced a row (schema 2 only)."""
    ids = sorted({c for c in call_ids if c})
    if not ids or not has_table(con, "art", "ember_calls"):
        return {}
    marks = ", ".join("?" for _ in ids)
    out = {}
    for r in con.execute(
        f"SELECT call_id, backend, region, effort, response_id, status, input_tokens, cached_tokens, output_tokens, "
        f"reasoning_tokens, cost_nano, shadow_nano, latency_ms FROM art.ember_calls WHERE call_id IN ({marks})", ids,
    ):
        row = dict(r)
        row["cost_usd"] = round((row.pop("cost_nano") or 0) / 1e9, 9)
        row["shadow_usd"] = round((row.pop("shadow_nano") or 0) / 1e9, 9)
        out[r["call_id"]] = row
    return out


def review_json(v: sqlite3.Row) -> dict[str, Any]:
    """One review in a fixed shape (schema 1 rows have no response id)."""
    r = dict(v)
    return {"region": r["region"], "verdict": r["verdict"], "reasons": json.loads(r["reasons"] or "[]"),
            "source": r["source"], "witness_id": r.get("witness_id"), "call_id": r.get("call_id"),
            "response_id": r.get("response_id")}


def artifact_cache_header() -> str:
    # Behind Basic Auth a shared cache (Cloudflare) must not keep these; after the unlock it may.
    if os.environ.get("EDDIE_PUBLIC_CACHE") == "1":
        return "public, max-age=31536000, immutable"
    return "max-age=31536000, immutable"


def create_router() -> APIRouter:
    data = ExplorerData()
    router = APIRouter(prefix="/api")

    @router.get("/overview")
    def overview() -> dict[str, Any]:
        def build() -> dict[str, Any]:
            with data.cone_con() as con:
                meta = dict(con.execute("SELECT key, value FROM cone_meta").fetchall())
                sorry = con.execute("SELECT COALESCE(SUM(has_sorry), 0) FROM idx.decls").fetchone()[0]
                files = con.execute("SELECT COUNT(*) FROM idx.modules").fetchone()[0]
                pillars = []
                for r in con.execute(
                    "SELECT p.*, d.full_name AS head_name FROM pillars p LEFT JOIN idx.decls d ON d.id = p.head_decl_id ORDER BY p.ord"
                ):
                    pillars.append({
                        "id": r["id"], "slug": r["slug"], "title": r["title"], "subtitle": r["subtitle"],
                        "category": r["category"], "ord": r["ord"], "flow_ord": r["flow_ord"], "chapter": r["flow_ord"] + 1,
                        "tier": r["tier"], "ref_in": r["ref_in"], "ref_out": r["ref_out"], "live": r["live"], "dead": r["dead"],
                        "decls": r["decls"], "modules": r["modules"],
                        "head": {"id": r["head_decl_id"], "full_name": r["head_name"]},
                        "timeline": json.loads(r["timeline"]),
                        "node_id": con.execute("SELECT id FROM tree_nodes WHERE kind = 'pillar' AND pillar_id = ?", (r["id"],)).fetchone()[0],
                    })
                cross = con.execute("SELECT source, target, weight FROM pillar_edges ORDER BY weight DESC LIMIT 60").fetchall()
                flows = [{"from": f["provider"], "to": f["consumer"], "net": f["net"], "forward": f["forward"],
                          "backward": f["backward"]} for f in ch.net_flows(con)[:60]]
                featured = []
                sources: dict[str, int] = {}
                if data.has_artifacts():
                    sources = dict(con.execute("SELECT source, COUNT(*) FROM art.witnesses GROUP BY source").fetchall())
                    featured = [
                        {"decl_id": r[0], "full_name": r[1], "kinds": (r[2] or "").split(","), "statuses": (r[3] or "").split(",")}
                        for r in con.execute(
                            "SELECT d.id, w.decl_full_name, GROUP_CONCAT(DISTINCT w.kind), GROUP_CONCAT(DISTINCT w.status) "
                            "FROM art.witnesses w JOIN idx.decls d ON d.full_name = w.decl_full_name "
                            "GROUP BY w.decl_full_name ORDER BY MIN(w.id) LIMIT 12"
                        )
                    ]
                roots = [
                    {"id": r[0], "full_name": r[1]}
                    for r in con.execute(
                        "SELECT m.decl_id, d.full_name FROM decl_metrics m JOIN idx.decls d ON d.id = m.decl_id "
                        "WHERE m.is_root = 1 ORDER BY m.decl_id"
                    )
                ]
            named = int(meta.get("named") or meta["decls"])
            dead_named = int(meta.get("dead_named") or meta["dead"])
            plan = None
            if data.plan.exists():
                raw = json.loads(data.plan.read_text())
                plan = {k: raw.get(k) for k in ("plan_hash", "fit", "planned_usd", "planned_expected_usd", "totals", "schedule", "coverage", "warnings")}
            return {
                "archive": {
                    "files": files, "decls": int(meta["decls"]), "live": int(meta["live"]), "dead": int(meta["dead"]),
                    "named": named, "dead_named": dead_named,
                    "dead_pct": round(100 * dead_named / max(named, 1), 1), "sorry": int(sorry),
                },
                "cone": {"edges": int(meta["edges"]), "live_edges": int(meta["live_edges"]),
                         "agreement": json.loads(meta.get("agreement") or "{}"), "max_depth": int(meta["max_depth"])},
                "roots": roots,
                "pillars": pillars,
                "cross_pillar_edges": [{"source": r[0], "target": r[1], "weight": r[2]} for r in cross],
                # net reference flow, provider -> consumer: "from" is built on by "to"
                "pillar_flows": flows,
                "flow": {"forward_pct": float(meta.get("flow_forward_pct") or 0.0), "method": meta.get("flow_method")},
                "featured": featured,
                "artifact_sources": sources,
                "plan": plan,
            }

        return data.cached("overview", data._mtime(data.cone, data.artifacts_db, data.plan), build)

    @router.get("/map/node/{node_id}")
    def map_node(
        node_id: int,
        limit: int = Query(160, ge=1, le=400),
        include: int | None = Query(None, ge=0, description="a child node id (or a decl id under a module) that must be returned"),
    ) -> dict[str, Any]:
        with data.cone_con() as con:
            node = con.execute("SELECT * FROM tree_nodes WHERE id = ?", (node_id,)).fetchone()
            if node is None:
                raise HTTPException(404, "no such node")
            counts = data.node_status_counts(con)
            crumbs = []
            cur = node
            while cur is not None:
                crumbs.append({"id": cur["id"], "label": cur["label"], "kind": cur["kind"]})
                cur = con.execute("SELECT * FROM tree_nodes WHERE id = ?", (cur["parent_id"],)).fetchone() if cur["parent_id"] is not None else None
            crumbs.reverse()

            def pack(r: sqlite3.Row) -> dict[str, Any]:
                c = counts.get(r["id"], {})
                return {"id": r["id"], "kind": r["kind"], "label": r["label"], "path": r["path"], "pillar_id": r["pillar_id"],
                        "module_id": r["module_id"], "modules": r["modules"], "decls": r["decls"], "live": r["live"],
                        "dead": r["dead"], "top_decl_id": r["top_decl_id"], "top_score": r["top_score"],
                        "verified": c.get("verified", 0), "triaged": c.get("triaged", 0)}

            if node["kind"] == "module":
                statuses = data.status_by_decl(con)
                decl_sql = (
                    "SELECT d.id, d.kind, d.name, d.full_name, d.start_line, m.live, m.score, m.is_root, m.dom_mass "
                    "FROM idx.decls d JOIN decl_metrics m ON m.decl_id = d.id WHERE d.module_id = ? "
                )
                rows = con.execute(decl_sql + "ORDER BY m.live DESC, m.score DESC, d.id LIMIT ?", (node["module_id"], limit)).fetchall()
                if include is not None and all(r["id"] != include for r in rows):
                    rows += con.execute(decl_sql + "AND d.id = ?", (node["module_id"], include)).fetchall()
                children = [
                    {"id": f"d{r['id']}", "decl_id": r["id"], "kind": "decl", "decl_kind": r["kind"],
                     "label": (r["name"] or r["kind"]).rsplit(".", 1)[-1], "full_name": r["full_name"],
                     "live": r["live"], "dead": 1 - r["live"], "decls": 1, "score": r["score"], "is_root": r["is_root"],
                     "dom_mass": r["dom_mass"], "status": statuses.get(r["id"], "live" if r["live"] else "dead")}
                    for r in rows
                ]
                total = con.execute("SELECT COUNT(*) FROM idx.decls WHERE module_id = ?", (node["module_id"],)).fetchone()[0]
            else:
                order = (
                    "ORDER BY COALESCE((SELECT ord FROM pillars WHERE id = t.pillar_id), 9999)"
                    if node["kind"] == "root"
                    else "ORDER BY t.live DESC, t.label"
                )
                rows = con.execute(f"SELECT * FROM tree_nodes t WHERE t.parent_id = ? {order} LIMIT ?", (node_id, limit)).fetchall()
                if include is not None and all(r["id"] != include for r in rows):
                    rows += con.execute("SELECT * FROM tree_nodes t WHERE t.id = ? AND t.parent_id = ?", (include, node_id)).fetchall()
                children = [pack(r) for r in rows]
                total = con.execute("SELECT COUNT(*) FROM tree_nodes WHERE parent_id = ?", (node_id,)).fetchone()[0]
            more = None
            if total > len(children):
                shown_live = sum(c.get("live", 0) for c in children)
                shown_decls = sum(c.get("decls", 0) for c in children)
                more = {"count": total - len(children), "live": node["live"] - shown_live, "decls": node["decls"] - shown_decls}
            return {"node": pack(node), "breadcrumb": crumbs, "children": children, "more": more}

    @router.get("/decl/{decl_id}")
    def decl(decl_id: int, body: bool = False) -> dict[str, Any]:
        with data.cone_con() as con:
            r = con.execute(
                "SELECT d.*, mo.name AS module, mo.path AS path, m.live, m.live_graph, m.depth, m.fan_in, m.fan_out, m.ppr, "
                "m.idom, m.dom_mass, m.bridge, m.score, m.pillar_rank, m.is_root, m.pillar_id, p.title AS pillar_title, "
                "p.slug AS pillar_slug, t.id AS node_id "
                "FROM idx.decls d JOIN idx.modules mo ON mo.id = d.module_id JOIN decl_metrics m ON m.decl_id = d.id "
                "LEFT JOIN pillars p ON p.id = m.pillar_id LEFT JOIN tree_nodes t ON t.kind = 'module' AND t.module_id = d.module_id "
                "WHERE d.id = ?",
                (decl_id,),
            ).fetchone()
            if r is None:
                raise HTTPException(404, "no such declaration")
            idom_name = None
            idom_id = None
            if r["idom"] and r["idom"] > 0:
                row = con.execute("SELECT full_name FROM idx.decls WHERE id = ?", (r["idom"],)).fetchone()
                idom_name = row[0] if row else "Main"
                idom_id = r["idom"] if row else None
            out: dict[str, Any] = {
                "id": r["id"], "kind": r["kind"], "name": r["name"], "full_name": r["full_name"], "namespace": r["namespace"],
                "signature": r["signature"], "docstring": r["docstring"], "context": r["context"], "attributes": r["attributes"],
                "modifiers": r["modifiers"], "body_kind": r["body_kind"],
                "citation": {"module": r["module"], "path": f"src/{r['path']}", "start_line": r["start_line"], "end_line": r["end_line"]},
                "metrics": {"live": bool(r["live"]), "reached_by_graph": bool(r["live_graph"]), "depth": r["depth"],
                            "fan_in": r["fan_in"], "fan_out": r["fan_out"], "ppr": r["ppr"], "dom_mass": r["dom_mass"],
                            "dominator": idom_name, "dominator_id": idom_id, "bridge": r["bridge"], "score": r["score"], "pillar_rank": r["pillar_rank"],
                            "is_root": bool(r["is_root"])},
                "pillar": {"id": r["pillar_id"], "title": r["pillar_title"], "slug": r["pillar_slug"]},
                "node_id": r["node_id"],
                "status": data.status_by_decl(con).get(decl_id, "live" if r["live"] else "dead"),
                "triage": None, "intuition": None, "witnesses": [], "reviews": [],
            }
            out["planned"] = None
            if data.has_artifacts():
                w = con.execute(
                    "SELECT tier, kind, scheduled_day, status FROM art.work_items WHERE decl_id = ? ORDER BY seq LIMIT 1",
                    (decl_id,),
                ).fetchone()
                if w is None:
                    w = con.execute(
                        "SELECT w.tier, w.kind, w.scheduled_day, w.status FROM art.work_items w, "
                        "json_each(w.payload, '$.decl_ids') j WHERE w.kind = 'triage_batch' AND j.value = ? LIMIT 1",
                        (decl_id,),
                    ).fetchone()
                out["planned"] = dict(w) if w else None
            out["source_available"] = data.source_available(con)
            out["attempts"] = []
            if data.has_artifacts() and r["full_name"]:
                fn = r["full_name"]
                t = con.execute("SELECT * FROM art.triage WHERE decl_full_name = ?", (fn,)).fetchone()
                if t:
                    t = dict(t)
                    out["triage"] = {k: t.get(k) for k in ("gloss", "source", "role", "area", "visualizability",
                                                           "visual_hint", "call_id", "response_id")}
                i = con.execute("SELECT * FROM art.intuitions WHERE decl_full_name = ?", (fn,)).fetchone()
                if i:
                    i = dict(i)
                    out["intuition"] = {
                        "intuition_md": i["intuition_md"], "verdict": i["verdict"], "source": i["source"],
                        "arbiter_notes": _json(i.get("arbiter_notes")), "citation": _json(i.get("citation")),
                        "call_id": i.get("call_id"), "response_id": i.get("response_id"),
                    }
                out["witnesses"] = [
                    witness_json(w)
                    for w in con.execute("SELECT * FROM art.witnesses WHERE decl_full_name = ? ORDER BY id", (fn,))
                ]
                out["reviews"] = [review_json(v) for v in con.execute(
                    "SELECT * FROM art.reviews WHERE decl_full_name = ? ORDER BY id", (fn,))]
                if has_table(con, "art", "witness_attempts"):
                    out["attempts"] = [
                        {**dict(a), "critique": _json(a["critique"]), "qc": _json(a["qc"])}
                        for a in con.execute(
                            "SELECT attempt, origin, effort, outcome, title, caption, relation, critique, qc, witness_id, source, "
                            "call_id, response_id FROM art.witness_attempts WHERE decl_full_name = ? ORDER BY attempt, id", (fn,))
                    ]
                calls = [x.get("call_id") for x in (out["triage"] or {}, out["intuition"] or {})]
                calls += [w.get("call_id") for w in out["witnesses"]] + [v.get("call_id") for v in out["reviews"]]
                calls += [a.get("call_id") for a in out["attempts"]]
                out["receipts"] = receipts(con, calls)
            if body:
                if not out["source_available"]:
                    out["body"] = None
                    out["body_note"] = "The Lean source is not served from this deployment: Eddie shows statements only."
                else:
                    meta = dict(con.execute("SELECT key, value FROM idx.index_meta").fetchall())
                    src = Path(meta["source_root"]) / r["path"]
                    if src.exists():
                        with open(src, "rb") as fh:
                            fh.seek(r["start_byte"])
                            out["body"] = fh.read(min(r["end_byte"] - r["start_byte"], 20_000)).decode("utf-8", "replace")
            return out

    @router.get("/decl/{decl_id}/neighbors")
    def neighbors(decl_id: int, limit: int = Query(12, ge=1, le=50)) -> dict[str, Any]:
        with data.cone_con() as con:
            def side(sql: str) -> list[dict[str, Any]]:
                return [dict(r) for r in con.execute(sql, (decl_id, limit))]

            uses = side(
                "SELECT d.id, d.full_name, d.kind, m.score, m.pillar_id, m.live FROM edges e JOIN idx.decls d ON d.id = e.dst "
                "JOIN decl_metrics m ON m.decl_id = e.dst WHERE e.src = ? ORDER BY m.live DESC, m.score DESC LIMIT ?"
            )
            used_by = side(
                "SELECT d.id, d.full_name, d.kind, m.score, m.pillar_id, m.live FROM edges e JOIN idx.decls d ON d.id = e.src "
                "JOIN decl_metrics m ON m.decl_id = e.src WHERE e.dst = ? ORDER BY m.live DESC, m.score DESC LIMIT ?"
            )
            return {"uses": uses, "used_by": used_by}

    @router.get("/artifacts/{name}")
    def artifact(name: str) -> FileResponse:
        if not ARTIFACT_NAME.fullmatch(name):
            raise HTTPException(404, "not an artifact name")
        path = (data.artifact_dir / name).resolve()
        if path.parent != data.artifact_dir.resolve() or not path.is_file():
            raise HTTPException(404, "no such artifact")
        return FileResponse(path, media_type=MEDIA[name.rsplit(".", 1)[1]], headers={"Cache-Control": artifact_cache_header()})

    @router.get("/activity")
    def activity() -> dict[str, Any]:
        if not data.has_artifacts():
            return {"region": None}
        con = sqlite3.connect(f"file:{data.artifacts_db}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT region, decl_full_name, message, source, updated_at FROM activity WHERE id = 1").fetchone()
        finally:
            con.close()
        return dict(row) if row else {"region": None}

    register_atlas(router, data)
    register_media(router)
    return router


def _json(raw: Any) -> Any:
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw
