"""Multi-core index build: parse every .lean file, resolve imports, compute DAG metrics."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from eddie.ingest.graph import compute_metrics, detect_source_root, find_project_root, module_name
from eddie.ingest.index_db import POST_INDEXES, SCHEMA
from eddie.ingest.lean_parser import PARSER_VERSION, parse_source

ROOT_MODULES = {"main": ("Main",), "statements": ("Statements",)}


def _parse_one(args: tuple[str, str]) -> tuple[tuple, list[tuple], list[str]]:
    """Worker: parse one file and return plain tuples (cheap to pickle)."""
    path, root = args
    rel = os.path.relpath(path, root)
    with open(path, "rb") as fh:
        rec = parse_source(rel, fh.read())
    module = (rec.path, rec.sha256, rec.bytes, rec.lines, len(rec.decls), rec.module_doc, json.dumps(rec.anomalies))
    decls = [
        (
            d.kind, d.name, d.full_name, d.namespace, " ".join(d.modifiers) or None, d.attributes,
            d.docstring, d.signature, d.context or None, d.universe_params, d.start_byte, d.sig_end_byte,
            d.end_byte, d.start_line, d.end_line, d.body_kind, d.body_bytes,
            len(d.signature.encode("utf-8")), int(d.has_sorry),
        )
        for d in rec.decls
    ]
    return module, decls, [imp.module for imp in rec.imports]


def build_index(
    src: Path,
    out: Path,
    *,
    workers: int | None = None,
    archive_sha256: str | None = None,
    log=print,
) -> dict[str, Any]:
    t0 = time.time()
    project = find_project_root(Path(src))
    src_root = detect_source_root(project)
    files = sorted(p for p in src_root.rglob("*.lean") if ".lake" not in p.parts)
    if not files:
        raise FileNotFoundError(f"no .lean files under {src_root}")
    workers = workers or max(1, (os.cpu_count() or 2) - 2)
    log(f"indexing {len(files):,} files under {src_root} with {workers} workers")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(SCHEMA)

    names: dict[str, int] = {}
    pending_imports: list[tuple[int, str]] = []
    n_decls = 0
    t_parse = time.time()
    args = [(str(f), str(src_root)) for f in files]
    ctx = mp.get_context("spawn")
    batch: list[tuple] = []
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        results = pool.map(_parse_one, args, chunksize=64) if workers > 1 else map(_parse_one, args)
        for module_id, (module, decls, imports) in enumerate(results, start=1):
            name = module_name(module[0])
            names[name] = module_id
            con.execute(
                "INSERT INTO modules (id, name, path, sha256, bytes, lines, n_decls, module_doc, anomalies) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (module_id, name, *module),
            )
            batch.extend((module_id, *d) for d in decls)
            n_decls += len(decls)
            pending_imports.extend((module_id, imp) for imp in imports)
            if len(batch) >= 50_000:
                _insert_decls(con, batch)
                batch.clear()
            if module_id % 20_000 == 0:
                log(f"  {module_id:,}/{len(files):,} files, {n_decls:,} decls")
    _insert_decls(con, batch)
    parse_s = time.time() - t_parse

    edges = []
    import_rows = []
    for src_id, dst_name in pending_imports:
        dst_id = names.get(dst_name)
        import_rows.append((src_id, dst_name, dst_id, 0 if dst_id else 1))
        if dst_id:
            edges.append((src_id - 1, dst_id - 1))
    con.executemany("INSERT INTO imports (src_id, dst_name, dst_id, is_external) VALUES (?, ?, ?, ?)", import_rows)

    main_roots = [names[r] - 1 for r in ROOT_MODULES["main"] if r in names]
    stmt_roots = [names[r] - 1 for r in ROOT_MODULES["statements"] if r in names]
    for r in main_roots:
        con.execute("UPDATE modules SET is_main_root = 1 WHERE id = ?", (r + 1,))
    for r in stmt_roots:
        con.execute("UPDATE modules SET is_statements_root = 1 WHERE id = ?", (r + 1,))

    t_graph = time.time()
    n = len(names)
    metrics = compute_metrics(n, edges, main_roots, stmt_roots)
    external = [0] * n
    for src_id, _, dst_id, _ in import_rows:
        if dst_id is None:
            external[src_id - 1] += 1
    con.executemany(
        "INSERT INTO module_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                i + 1, int(metrics.in_degree[i]), int(metrics.out_degree[i]), external[i],
                int(metrics.dist_main[i]), int(metrics.dist_statements[i]), int(metrics.depth[i]),
                int(metrics.height[i]), float(metrics.pagerank[i]), int(metrics.in_cycle[i]),
            )
            for i in range(n)
        ),
    )
    graph_s = time.time() - t_graph

    t_fts = time.time()
    con.executescript(POST_INDEXES)
    con.execute("INSERT INTO decl_fts(decl_fts) VALUES ('rebuild')")
    fts_s = time.time() - t_fts

    meta = {
        "parser_version": PARSER_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_root": str(src_root.resolve()),
        "project_root": str(project.resolve()),
        "files": str(len(files)),
        "decls": str(n_decls),
        "edges": str(len(edges)),
        "archive_sha256": archive_sha256 or "",
        "main_roots": ",".join(ROOT_MODULES["main"]),
        "statements_roots": ",".join(ROOT_MODULES["statements"]),
        "timing_parse_s": f"{parse_s:.2f}",
        "timing_graph_s": f"{graph_s:.2f}",
        "timing_fts_s": f"{fts_s:.2f}",
        "timing_total_s": f"{time.time() - t0:.2f}",
        "workers": str(workers),
    }
    con.executemany("INSERT INTO index_meta (key, value) VALUES (?, ?)", meta.items())
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("ANALYZE")
    con.close()
    os.replace(tmp, out)
    log(f"index written to {out}: {len(files):,} modules, {n_decls:,} decls, {len(edges):,} import edges in {time.time() - t0:.1f}s")
    return meta


def _insert_decls(con: sqlite3.Connection, rows: list[tuple]) -> None:
    if rows:
        con.executemany(
            "INSERT INTO decls (module_id, kind, name, full_name, namespace, modifiers, attributes, docstring, "
            "signature, context, universe_params, start_byte, sig_end_byte, end_byte, start_line, end_line, "
            "body_kind, body_bytes, sig_bytes, has_sorry) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
