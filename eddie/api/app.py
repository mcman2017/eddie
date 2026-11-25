"""Eddie's read-only HTTP API.

The API serves precomputed data: health, the governor's public spend snapshot, the
telemetry export, index stats and search, plus the built web UI. It never constructs an
LLM gateway, never holds a key and never opens the spend ledger, so nothing reachable over
HTTP can spend money.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from eddie import __version__, paths
from eddie.ingest.index_db import connect_ro, lookup, search, stats


def _paths() -> tuple[Path, Path, Path]:
    public = Path(os.environ.get("EDDIE_PUBLIC_DIR") or paths.default_public_dir())
    index = Path(os.environ.get("EDDIE_INDEX_PATH") or paths.default_index_path())
    web = Path(os.environ.get("EDDIE_WEB_DIST") or paths.repo_root() / "web" / "dist")
    return public, index, web


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def create_app() -> FastAPI:
    public_dir, index_path, web_dist = _paths()
    app = FastAPI(title="Eddie", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    stats_cache: dict[str, Any] = {}
    stats_lock = threading.Lock()

    def index_con() -> sqlite3.Connection:
        if not index_path.exists():
            raise HTTPException(503, "index not built: run `python -m eddie.cli index build`")
        return connect_ro(index_path)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "index": index_path.exists(), "web": web_dist.exists()}

    @app.get("/api/budget")
    def budget() -> dict[str, Any]:
        snapshot = _read_json(public_dir / "status.json")
        return {"available": snapshot is not None, **(snapshot or {})}

    @app.get("/api/telemetry")
    def telemetry() -> dict[str, Any]:
        trace = _read_json(public_dir / "telemetry.json")
        return {"available": trace is not None, **(trace or {})}

    @app.get("/api/index/stats")
    def index_stats() -> dict[str, Any]:
        mtime = index_path.stat().st_mtime if index_path.exists() else None
        with stats_lock:
            if stats_cache.get("mtime") != mtime or "data" not in stats_cache:
                with index_con() as con:
                    stats_cache["data"] = stats(con)
                stats_cache["mtime"] = mtime
            return stats_cache["data"]

    @app.get("/api/search")
    def api_search(
        q: str = Query(..., min_length=1, max_length=200),
        limit: int = Query(20, ge=1, le=50),
        kind: str | None = Query(None, max_length=20),
    ) -> dict[str, Any]:
        with index_con() as con:
            return {"query": q, "results": search(con, q, limit=limit, kind=kind)}

    @app.get("/api/decl")
    def api_decl(name: str = Query(..., min_length=1, max_length=400)) -> dict[str, Any]:
        with index_con() as con:
            rows = lookup(con, name)
        keys = ("kind", "full_name", "signature", "docstring", "context", "module", "path", "start_line", "end_line", "body_kind")
        return {"name": name, "matches": [{k: r[k] for k in keys} for r in rows[:10]]}

    if web_dist.exists():
        assets = web_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(404)
            candidate = (web_dist / full_path).resolve()
            if full_path and candidate.is_file() and str(candidate).startswith(str(web_dist.resolve())):
                return FileResponse(candidate)
            # index.html must never be cached, or long-lived tabs keep an old API contract
            return FileResponse(web_dist / "index.html", headers={"Cache-Control": "no-store"})
    else:

        @app.get("/", include_in_schema=False)
        def root() -> JSONResponse:
            return JSONResponse({"eddie": __version__, "ui": "web/dist not built (npm --prefix web run build)"})

    return app


app = create_app()
