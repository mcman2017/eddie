"""SQLite index: modules, import edges, declarations (with FTS5) and module graph metrics.

The index is rebuildable and read-mostly. It is built into a temp file and renamed into
place atomically. It uses the default rollback journal rather than WAL, so it can be
mounted read-only into containers.
"""

from __future__ import annotations

import gzip
import re
import sqlite3
from pathlib import Path
from typing import Any

from eddie.config import NANO_PER_USD
from eddie.llm import pricing

SCHEMA = """
CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE modules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    lines INTEGER NOT NULL,
    n_decls INTEGER NOT NULL,
    module_doc TEXT,
    anomalies TEXT,
    is_main_root INTEGER NOT NULL DEFAULT 0,
    is_statements_root INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE imports (
    src_id INTEGER NOT NULL REFERENCES modules(id),
    dst_name TEXT NOT NULL,
    dst_id INTEGER REFERENCES modules(id),
    is_external INTEGER NOT NULL
);
CREATE TABLE decls (
    id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES modules(id),
    kind TEXT NOT NULL,
    name TEXT,
    full_name TEXT,
    namespace TEXT,
    modifiers TEXT,
    attributes TEXT,
    docstring TEXT,
    signature TEXT NOT NULL,
    context TEXT,
    universe_params TEXT,
    start_byte INTEGER NOT NULL,
    sig_end_byte INTEGER NOT NULL,
    end_byte INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    body_kind TEXT NOT NULL,
    body_bytes INTEGER NOT NULL,
    sig_bytes INTEGER NOT NULL,
    has_sorry INTEGER NOT NULL
);
CREATE TABLE module_metrics (
    module_id INTEGER PRIMARY KEY REFERENCES modules(id),
    in_degree INTEGER NOT NULL,
    out_degree INTEGER NOT NULL,
    external_imports INTEGER NOT NULL,
    dist_main INTEGER NOT NULL,
    dist_statements INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    height INTEGER NOT NULL,
    pagerank REAL NOT NULL,
    in_cycle INTEGER NOT NULL
);
CREATE VIRTUAL TABLE decl_fts USING fts5(
    full_name, signature, docstring,
    content='decls', content_rowid='id',
    tokenize="unicode61 remove_diacritics 0 tokenchars '_'"
);
"""

POST_INDEXES = """
CREATE INDEX decls_full_name ON decls(full_name);
CREATE INDEX decls_name ON decls(name);
CREATE INDEX decls_module ON decls(module_id);
CREATE INDEX imports_src ON imports(src_id);
CREATE INDEX imports_dst ON imports(dst_id);
"""


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def fts_query(text: str) -> str | None:
    """Quote every user term (no FTS syntax injection); each term matches as a prefix."""
    terms = [t for t in re.split(r"[\s.]+", text.strip()) if t]
    if not terms:
        return None
    return " ".join('"' + t.replace('"', '""') + '"*' for t in terms)


def search(con: sqlite3.Connection, query: str, *, limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[int] = set()
    q = fts_query(query)
    kind_sql = " AND d.kind = ?" if kind else ""
    if q:
        params: list[Any] = [q] + ([kind] if kind else []) + [limit]
        rows = con.execute(
            "SELECT d.id, d.kind, d.full_name, d.signature, m.name AS module, bm25(decl_fts) AS score "
            "FROM decl_fts JOIN decls d ON d.id = decl_fts.rowid JOIN modules m ON m.id = d.module_id "
            f"WHERE decl_fts MATCH ?{kind_sql} ORDER BY score LIMIT ?",
            params,
        ).fetchall()
        for r in rows:
            seen.add(r["id"])
            results.append(dict(r))
    if len(results) < limit and query.strip():
        pattern = "%" + query.strip().replace("%", "").replace("_", r"\_") + "%"
        params = [pattern] + ([kind] if kind else []) + [limit * 2]
        rows = con.execute(
            "SELECT d.id, d.kind, d.full_name, d.signature, m.name AS module, NULL AS score "
            "FROM decls d JOIN modules m ON m.id = d.module_id "
            rf"WHERE d.full_name LIKE ? ESCAPE '\'{kind_sql} LIMIT ?",
            params,
        ).fetchall()
        for r in rows:
            if r["id"] not in seen and len(results) < limit:
                seen.add(r["id"])
                results.append(dict(r))
    return results


def lookup(con: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    rows = con.execute(
        "SELECT d.*, m.name AS module, m.path AS path FROM decls d JOIN modules m ON m.id = d.module_id "
        "WHERE d.full_name = ? OR d.name = ? ORDER BY d.id LIMIT 20",
        (name, name),
    ).fetchall()
    if rows:
        return rows
    return con.execute(
        "SELECT d.*, m.name AS module, m.path AS path FROM decls d JOIN modules m ON m.id = d.module_id "
        "WHERE d.full_name LIKE ? ORDER BY d.id LIMIT 20",
        ("%." + name,),
    ).fetchall()


def load_body(con: sqlite3.Connection, row: sqlite3.Row) -> str:
    meta = dict(con.execute("SELECT key, value FROM index_meta").fetchall())
    source = Path(meta["source_root"]) / row["path"]
    with open(source, "rb") as fh:
        fh.seek(row["start_byte"])
        return fh.read(row["end_byte"] - row["start_byte"]).decode("utf-8", "replace")


def stats(con: sqlite3.Connection, *, output_tokens_per_decl: int = 16, typical_bytes_per_token: float = 3.5) -> dict[str, Any]:
    meta = dict(con.execute("SELECT key, value FROM index_meta").fetchall())
    kinds = {r[0]: r[1] for r in con.execute("SELECT kind, COUNT(*) FROM decls GROUP BY kind ORDER BY 2 DESC")}
    agg = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(sig_bytes),0), COALESCE(SUM(body_bytes),0), COALESCE(SUM(has_sorry),0) FROM decls"
    ).fetchone()
    n_decls, sig_bytes, body_bytes, sorry = agg
    mods = con.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0), COALESCE(SUM(lines),0) FROM modules").fetchone()
    ext = con.execute("SELECT COUNT(DISTINCT dst_name) FROM imports WHERE is_external = 1").fetchone()[0]
    ext_roots = con.execute(
        "SELECT substr(dst_name, 1, instr(dst_name || '.', '.') - 1) AS root, COUNT(*) FROM imports "
        "WHERE is_external = 1 GROUP BY root ORDER BY 2 DESC LIMIT 5"
    ).fetchall()
    graph = con.execute(
        "SELECT SUM(dist_main >= 0), MAX(depth), MAX(height), SUM(in_cycle), COUNT(*) FROM module_metrics"
    ).fetchone()
    top_pr = con.execute(
        "SELECT m.name, mm.pagerank, mm.in_degree FROM module_metrics mm JOIN modules m ON m.id = mm.module_id "
        "ORDER BY mm.pagerank DESC LIMIT 10"
    ).fetchall()
    anomalies = con.execute("SELECT COUNT(*) FROM modules WHERE anomalies IS NOT NULL AND anomalies <> '[]'").fetchone()[0]

    # Forecast: one Ember R1 triage pass over every signature.
    bound_tokens = sig_bytes  # provable upper bound (bytes >= tokens)
    typical_tokens = int(sig_bytes / typical_bytes_per_token)
    out_tokens = n_decls * output_tokens_per_decl
    forecast = {
        "signature_bytes": sig_bytes,
        "input_usd_upper_bound": bound_tokens * pricing.TIER_1.input_nano / NANO_PER_USD,
        "input_usd_typical": typical_tokens * pricing.TIER_1.input_nano / NANO_PER_USD,
        "output_usd_at_tokens_per_decl": out_tokens * pricing.TIER_1.output_nano / NANO_PER_USD,
        "output_tokens_per_decl": output_tokens_per_decl,
    }
    forecast["total_usd_typical"] = forecast["input_usd_typical"] + forecast["output_usd_at_tokens_per_decl"]
    return {
        "meta": meta,
        "modules": mods[0],
        "source_bytes": mods[1],
        "source_lines": mods[2],
        "decls": n_decls,
        "decls_by_kind": kinds,
        "sorry_count": sorry,
        "files_with_anomalies": anomalies,
        "external_modules": ext,
        "external_roots": [(r[0], r[1]) for r in ext_roots],
        "reachable_from_main": graph[0] or 0,
        "max_depth": graph[1],
        "max_height": graph[2],
        "modules_in_cycles": graph[3] or 0,
        "top_pagerank": [(r[0], round(r[1], 6), r[2]) for r in top_pr],
        "body_bytes_stripped": body_bytes,
        "forecast_r1_all_signatures": forecast,
    }


_PRIVATE = re.compile(r"^_private\.(?:[^.]+\.)*?0\.(.+)$")


_GENERATED_LAST = re.compile(
    r"^(?:(?:eq|match|proof|cstage|spec|simp)_\d+(?:_\d+)*|eq_def|mk|rec|recOn|casesOn|noConfusion|noConfusionType|below|brecOn|"
    r"ibelow|binductionOn|sizeOf_spec|injEq|inj|ext|ext_iff|congr_simp|induct|elim|eq_iff|\d+)$"
)


def validate_against_spine(
    con: sqlite3.Connection, spine_path: Path, project_roots: list[str] | None = None
) -> dict[str, Any]:
    """Recall of our parsed names against the exported proof spine (name<TAB>kind per line)."""
    names: set[str] = set()
    opener = gzip.open if str(spine_path).endswith(".gz") else open
    with opener(spine_path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            name = line.split("\t", 1)[0].strip()
            if not name:
                continue
            names.add(name)
            m = _PRIVATE.match(name)
            if m:
                names.add(m.group(1))
    per_kind: dict[str, dict[str, int]] = {}
    misses: list[str] = []
    for kind, full_name in con.execute("SELECT kind, full_name FROM decls WHERE full_name IS NOT NULL"):
        bucket = per_kind.setdefault(kind, {"ours": 0, "found": 0})
        bucket["ours"] += 1
        candidate = full_name.replace("«", "").replace("»", "")
        if full_name in names or candidate in names:
            bucket["found"] += 1
        elif len(misses) < 15:
            misses.append(f"{kind} {full_name}")
    for bucket in per_kind.values():
        bucket["recall_pct"] = round(100 * bucket["found"] / bucket["ours"], 2) if bucket["ours"] else 0.0
    total_ours = sum(b["ours"] for b in per_kind.values())
    total_found = sum(b["found"] for b in per_kind.values())

    # Parser accuracy, measured in the other direction: what fraction of the cone's
    # hand-written, project-local names did the parser find? (The direction above mixes
    # parser errors with source declarations the proof never uses.)
    ours = {r[0].replace("«", "").replace("»", "") for r in con.execute("SELECT full_name FROM decls WHERE full_name IS NOT NULL")}
    type_like = {
        r[0] for r in con.execute(
            "SELECT full_name FROM decls WHERE kind IN ('structure', 'class', 'inductive') AND full_name IS NOT NULL"
        )
    }
    roots = set(project_roots) if project_roots else {n.split(".", 1)[0] for n in ours}
    considered = covered = 0
    cone_misses: list[str] = []
    for name in names:
        if name.startswith("_private.") or name.split(".", 1)[0] not in roots:
            continue
        parts = name.split(".")
        last = parts[-1]
        if any(p.startswith("_") for p in parts) or _GENERATED_LAST.match(last) or last.startswith("inst"):
            continue
        if ".".join(parts[:-1]) in type_like:  # constructors / projections of our structures
            continue
        considered += 1
        if name in ours:
            covered += 1
        elif len(cone_misses) < 15:
            cone_misses.append(name)
    return {
        "spine_names": len(names),
        "our_named_decls": total_ours,
        "found_in_spine": total_found,
        "source_decls_in_proof_cone_pct": round(100 * total_found / total_ours, 2) if total_ours else 0.0,
        "per_kind": per_kind,
        "sample_source_decls_outside_cone": misses,
        "cone_handwritten_names_considered": considered,
        "cone_coverage_by_parser_pct": round(100 * covered / considered, 2) if considered else 0.0,
        "sample_cone_names_not_parsed": cone_misses,
    }
