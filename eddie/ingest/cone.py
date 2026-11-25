"""Build the declaration-level live proof cone: `data/index/eddie_cone.sqlite`.

Inputs: the immutable structural index (`eddie_index.sqlite`) and the source files it points at.
Optional: the export spine (`p0/spine.tsv.gz`) as the exact liveness oracle, and the companion
PDF for pillar titles.

The output is a rebuildable analysis DB. It holds reference edges, liveness (oracle and
graph), personalized PageRank from the roots, the dominator tree and the mass each node
dominates, cross-pillar bridges, a milestone score, the Proof Pillars, and the zoom tree
(root → pillar → directory → module) that the explorer renders. The index is never modified.

Two orders live in `pillars`: `ord` (the top-level reduction first, then by median depth) keeps
driving the zoom tree and the budget planner, and `flow_ord` is the chapter order of the atlas,
from the most foundational pillar to Main (see `eddie.planner.pillars.pillar_flow`).
"""

from __future__ import annotations

import gzip
import json
import multiprocessing as mp
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from eddie.ingest import graph as g
from eddie.ingest import treegroups as tg
from eddie.ingest.lean_parser import PARSER_VERSION
from eddie.ingest.refgraph import init_worker, module_edges
from eddie.planner import pillars as pl

CONE_VERSION = "2"
ROOT_MODULE = "Main"
_PRIVATE = re.compile(r"^_private\.(?:[^.]+\.)*?0\.(.+)$")

SCHEMA = """
CREATE TABLE cone_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE edges (src INTEGER NOT NULL, dst INTEGER NOT NULL, weak INTEGER NOT NULL);
CREATE TABLE decl_metrics (
    decl_id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL,
    pillar_id INTEGER NOT NULL,
    is_root INTEGER NOT NULL,
    live INTEGER NOT NULL,
    live_spine INTEGER,
    live_graph INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    fan_in INTEGER NOT NULL,
    fan_out INTEGER NOT NULL,
    ppr REAL NOT NULL,
    idom INTEGER NOT NULL,
    dom_mass INTEGER NOT NULL,
    bridge INTEGER NOT NULL,
    aux INTEGER NOT NULL,
    score REAL NOT NULL,
    pillar_rank INTEGER
);
CREATE TABLE module_pillar (module_id INTEGER PRIMARY KEY, pillar_id INTEGER NOT NULL);
CREATE TABLE pillars (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    subtitle TEXT,
    category TEXT,
    ord INTEGER NOT NULL,
    prefixes TEXT NOT NULL,
    modules INTEGER NOT NULL,
    decls INTEGER NOT NULL,
    live INTEGER NOT NULL,
    dead INTEGER NOT NULL,
    head_decl_id INTEGER,
    median_depth REAL,
    timeline TEXT NOT NULL,
    keywords TEXT NOT NULL,
    flow_ord INTEGER NOT NULL,
    tier TEXT NOT NULL,
    ref_in INTEGER NOT NULL,
    ref_out INTEGER NOT NULL,
    max_dom_share REAL NOT NULL
);
CREATE TABLE timeline (
    number INTEGER PRIMARY KEY,
    phrase TEXT NOT NULL,
    pillar_id INTEGER,
    candidate_pillar_id INTEGER,
    score REAL NOT NULL,
    margin REAL NOT NULL
);
CREATE TABLE pillar_edges (source INTEGER NOT NULL, target INTEGER NOT NULL, weight INTEGER NOT NULL);
CREATE TABLE tree_nodes (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    path TEXT NOT NULL,
    pillar_id INTEGER,
    module_id INTEGER,
    modules INTEGER NOT NULL,
    decls INTEGER NOT NULL,
    live INTEGER NOT NULL,
    dead INTEGER NOT NULL,
    top_decl_id INTEGER,
    top_score REAL NOT NULL
);
"""

POST = """
CREATE INDEX edges_src ON edges(src);
CREATE INDEX edges_dst ON edges(dst);
CREATE INDEX metrics_pillar ON decl_metrics(pillar_id, score DESC);
CREATE INDEX metrics_pillar_dom ON decl_metrics(pillar_id, dom_mass DESC);
CREATE INDEX metrics_module ON decl_metrics(module_id);
CREATE INDEX tree_parent ON tree_nodes(parent_id);
CREATE INDEX tree_module ON tree_nodes(module_id);
"""


def load_spine(path: Path) -> set[str]:
    names: set[str] = set()
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            name = line.split("\t", 1)[0].strip()
            if name:
                names.add(name)
                m = _PRIVATE.match(name)
                if m:
                    names.add(m.group(1))
    return names


def _pct(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Percentile rank in [0, 1] among `mask` entries (0 elsewhere); ties share a rank."""
    out = np.zeros(len(values))
    idx = np.flatnonzero(mask)
    if len(idx) <= 1:
        out[idx] = 1.0
        return out
    v = values[idx]
    order = np.argsort(v, kind="stable")
    ranks = np.empty(len(v))
    ranks[order] = np.arange(len(v))
    # equal values get the mean rank of their run
    sorted_v = v[order]
    boundaries = np.flatnonzero(np.diff(sorted_v)) + 1
    starts = np.concatenate([[0], boundaries])
    ends = np.concatenate([boundaries, [len(v)]])
    for s0, e0 in zip(starts, ends, strict=True):
        if e0 - s0 > 1:
            ranks[order[s0:e0]] = (s0 + e0 - 1) / 2
    out[idx] = ranks / (len(v) - 1)
    return out


def _slug(text: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "pillar"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def build_cone(
    index_path: Path,
    out_path: Path,
    *,
    spine_path: Path | None = None,
    pdf_path: Path | None = None,
    workers: int | None = None,
    log=print,
) -> dict[str, Any]:
    t0 = time.time()
    idx = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    meta = dict(idx.execute("SELECT key, value FROM index_meta"))
    if meta.get("parser_version") != PARSER_VERSION:
        raise RuntimeError(
            f"index was built by parser v{meta.get('parser_version')}, current is v{PARSER_VERSION}: rebuild the index first"
        )
    source_root = meta["source_root"]
    n_ids = idx.execute("SELECT COALESCE(MAX(id), 0) FROM decls").fetchone()[0] + 1
    modules = idx.execute(
        "SELECT m.id, m.name, m.path, MIN(d.id), COUNT(d.id) FROM modules m LEFT JOIN decls d ON d.module_id = m.id "
        "GROUP BY m.id ORDER BY m.id"
    ).fetchall()
    decl_rows = idx.execute("SELECT id, module_id, kind, full_name, body_bytes FROM decls ORDER BY id").fetchall()
    module_of = np.zeros(n_ids, dtype=np.int64)
    body_bytes = np.zeros(n_ids, dtype=np.int64)
    kinds: list[str] = [""] * n_ids
    names: list[str | None] = [None] * n_ids
    for decl_id, module_id, kind, full_name, bb in decl_rows:
        module_of[decl_id] = module_id
        body_bytes[decl_id] = bb
        kinds[decl_id] = kind
        names[decl_id] = full_name
    root_module = next((m for m in modules if m[1] == ROOT_MODULE), None)
    if root_module is None or not root_module[4]:
        raise RuntimeError(f"no `{ROOT_MODULE}` module with declarations in the index")
    roots = [d for d in range(root_module[3], root_module[3] + root_module[4]) if kinds[d] in ("theorem", "lemma", "def")]
    log(f"cone: {len(decl_rows):,} decls in {len(modules):,} modules; roots: {[names[r] for r in roots]}")

    # 1) reference edges (spawned workers re-parse each module against the global name table)
    t_edges = time.time()
    tasks = [(m[0], source_root, m[2], m[3], m[4]) for m in modules if m[4]]
    workers = workers or max(1, (os.cpu_count() or 2) - 2)
    src_parts, dst_parts, weak_parts, problems = [], [], [], []
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=init_worker, initargs=(str(index_path),)) as pool:
        for done, (_module_id, s_b, d_b, w_b, err) in enumerate(pool.map(module_edges, tasks, chunksize=32), 1):
            if err:
                problems.append(err)
                continue
            src_parts.append(np.frombuffer(s_b, dtype=np.uint32))
            dst_parts.append(np.frombuffer(d_b, dtype=np.uint32))
            weak_parts.append(np.frombuffer(w_b, dtype=np.uint8))
            if done % 25_000 == 0:
                log(f"  edges: {done:,}/{len(tasks):,} modules")
    if problems:
        raise RuntimeError(f"{len(problems)} modules no longer match the index (first: {problems[0]}); rebuild the index")
    src = np.concatenate(src_parts).astype(np.int64) if src_parts else np.zeros(0, dtype=np.int64)
    dst = np.concatenate(dst_parts).astype(np.int64) if dst_parts else np.zeros(0, dtype=np.int64)
    weak = np.concatenate(weak_parts) if weak_parts else np.zeros(0, dtype=np.uint8)
    edges_s = time.time() - t_edges
    log(f"  {len(src):,} reference edges ({int(weak.sum()):,} weak) in {edges_s:.1f}s")

    # 2) liveness: the export spine is the oracle when present; graph BFS is always computed
    ptr, targets = g._csr(n_ids, src, dst)
    reach = g._bfs(n_ids, ptr, targets, roots) >= 0
    live_spine = None
    if spine_path and Path(spine_path).exists():
        spine = load_spine(Path(spine_path))
        live_spine = np.zeros(n_ids, dtype=bool)
        for d in range(1, n_ids):
            name = names[d]
            if name and (name in spine or name.replace("«", "").replace("»", "") in spine):
                live_spine[d] = True
        live_spine[roots] = True
    live = live_spine if live_spine is not None else reach.copy()
    exists = np.array([bool(kinds[d]) for d in range(n_ids)])
    live &= exists
    agreement = {}
    if live_spine is not None:
        both = int((reach & live_spine).sum())
        agreement = {
            "graph_live": int(reach.sum()),
            "spine_live": int(live_spine.sum()),
            "precision": round(both / max(int(reach.sum()), 1), 4),
            "recall": round(both / max(int(live_spine.sum()), 1), 4),
        }
        log(f"  liveness: spine {agreement['spine_live']:,}, graph {agreement['graph_live']:,}, "
            f"precision {agreement['precision']:.3f}, recall {agreement['recall']:.3f}")

    # 3) metrics on the live subgraph
    t_metrics = time.time()
    keep = live[src] & live[dst]
    ls, ld = src[keep], dst[keep]
    fan_out = np.bincount(ls, minlength=n_ids)
    fan_in = np.bincount(ld, minlength=n_ids)
    lptr, ltargets = g._csr(n_ids, ls, ld)
    depth = g._bfs(n_ids, lptr, ltargets, roots)
    ppr = g.personalized_pagerank(n_ids, ls, ld, roots)
    idom, postorder = g.immediate_dominators(n_ids, ls, ld, roots)
    dom_mass = g.dominated_mass(n_ids, idom, postorder)
    log(f"  metrics (PPR, dominators over {int(keep.sum()):,} live edges) in {time.time() - t_metrics:.1f}s")

    # 4) pillars over live mass per module
    module_ids = [m[0] for m in modules]
    module_names = [m[1] for m in modules]
    live_per_module = np.bincount(module_of[live], minlength=max(module_ids) + 1)
    total_per_module = np.bincount(module_of[exists], minlength=max(module_ids) + 1)
    specs = pl.partition({name: int(live_per_module[mid]) for mid, name in zip(module_ids, module_names, strict=True)})
    module_pillar_list = pl.assign_modules(module_names, specs)
    module_pillar = np.full(max(module_ids) + 1, -1, dtype=np.int64)
    for mid, p in zip(module_ids, module_pillar_list, strict=True):
        module_pillar[mid] = p
    decl_pillar = module_pillar[module_of]

    # 5) bridges and scores
    cross = decl_pillar[ls] != decl_pillar[ld]
    pairs = np.unique(np.stack([ld[cross], decl_pillar[ls][cross]], axis=1), axis=0) if cross.any() else np.zeros((0, 2), dtype=np.int64)
    bridge = np.bincount(pairs[:, 0], minlength=n_ids) if len(pairs) else np.zeros(n_ids, dtype=np.int64)
    aux = np.array([kinds[d] in ("theorem", "lemma") for d in range(n_ids)]) & (body_bytes < 200) & (fan_in <= 1)
    score = (0.50 * _pct(ppr, live) + 0.35 * _pct(np.log1p(dom_mass.astype(float)), live) + 0.15 * _pct(bridge.astype(float), live))
    score = np.where(aux, 0.25 * score, score)
    score[~live] = 0.0
    score[roots] = 1.0

    # pillar vocabulary, timeline and heads
    top_names: dict[int, list[str]] = defaultdict(list)
    order_all = np.argsort(-score, kind="stable")
    for d in order_all[: min(len(order_all), 20_000)]:
        if score[d] <= 0:
            break
        p = int(decl_pillar[d])
        if names[d] and len(top_names[p]) < 60:
            top_names[p].append(names[d])
    vocab = pl.pillar_vocabulary(module_names, module_pillar_list, [int(live_per_module[m]) for m in module_ids], top_names, len(specs))
    items: list[pl.TimelineItem] = []
    matches: list[pl.TimelineMatch] = []
    if pdf_path and Path(pdf_path).exists():
        items = pl.timeline_items(pl.extract_pdf_text(Path(pdf_path)))
        matches = pl.match_timeline(specs, vocab, items)
    log(f"  pillars: {len(specs)} ({len(items)} timeline steps found in the PDF, "
        f"{sum(len(s.timeline) for s in specs)} placed with confidence)")

    pillar_rank = np.zeros(n_ids, dtype=np.int64)
    heads: dict[int, int] = {}
    median_depth: dict[int, float] = {}
    dom_share: dict[int, float] = {}
    live_total = max(int(live.sum()), 1)
    for p in range(len(specs)):
        members = np.flatnonzero((decl_pillar == p) & live)
        if not len(members):
            continue
        ranked = members[np.argsort(-score[members], kind="stable")]
        pillar_rank[ranked] = np.arange(1, len(ranked) + 1)
        entry = [d for d in ranked[:5000] if bridge[d] > 0 and kinds[d] in ("theorem", "lemma", "def", "abbrev")]
        heads[p] = int(entry[0] if entry else ranked[0])
        d_ok = depth[members][depth[members] >= 0]
        median_depth[p] = float(np.median(d_ok)) if len(d_ok) else 1e9
        dom_share[p] = float(dom_mass[members].max()) / live_total
    ordering = sorted(range(len(specs)), key=lambda p: (p != 0, median_depth.get(p, 1e9), -specs[p].live))

    # chapter order: the directed flow of references between pillars (provider before consumer)
    k = len(specs)
    ps, pt = decl_pillar[ls], decl_pillar[ld]
    sel = (ps != pt) & (ps >= 0) & (pt >= 0)
    pillar_weights = np.bincount(ps[sel] * k + pt[sel], minlength=k * k).reshape(k, k) if sel.any() else np.zeros((k, k), dtype=np.int64)
    flow = pl.pillar_flow(pillar_weights, dom_share)
    log(f"  flow: {100 * flow.forward_frac:.1f}% of {flow.total:,} cross-pillar references point forward "
        f"({'exact' if flow.exact else 'greedy'} order)")

    # 6) write the cone DB
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO edges VALUES (?, ?, ?)", zip(src.tolist(), dst.tolist(), weak.tolist(), strict=True))
    used: set[str] = set()
    for rank_order, p in enumerate(ordering):
        spec = specs[p]
        members = decl_pillar == p
        subtitle = None
        if spec.timeline:
            subtitle = " · ".join(f"#{it.number} {it.phrase}" for it in spec.timeline)
        con.execute(
            "INSERT INTO pillars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p, _slug(spec.title, used), spec.title, subtitle, spec.category, rank_order, json.dumps(spec.prefixes),
                int(sum(1 for x in module_pillar_list if x == p)), int((members & exists).sum()), int((members & live).sum()),
                int((members & exists & ~live).sum()), heads.get(p), median_depth.get(p),
                json.dumps([{"number": it.number, "phrase": it.phrase} for it in spec.timeline]), json.dumps(spec.keywords),
                flow.rank[p], flow.tier[p], flow.ref_in[p], flow.ref_out[p], round(dom_share.get(p, 0.0), 6),
            ),
        )
    phrase = {it.number: it.phrase for it in items}
    con.executemany(
        "INSERT INTO timeline VALUES (?, ?, ?, ?, ?, ?)",
        [(m.number, phrase[m.number], m.pillar, m.candidate, m.score, m.margin) for m in matches],
    )
    con.executemany("INSERT INTO module_pillar VALUES (?, ?)", zip(module_ids, module_pillar_list, strict=True))
    src_p, dst_p = np.nonzero(pillar_weights)
    con.executemany(
        "INSERT INTO pillar_edges VALUES (?, ?, ?)",
        [(int(a), int(b), int(pillar_weights[a, b])) for a, b in zip(src_p.tolist(), dst_p.tolist(), strict=True)],
    )
    is_root = np.zeros(n_ids, dtype=bool)
    is_root[roots] = True
    rows = []
    for d in range(1, n_ids):
        if not kinds[d]:
            continue
        rows.append((
            d, int(module_of[d]), int(decl_pillar[d]), int(is_root[d]), int(live[d]),
            None if live_spine is None else int(live_spine[d]), int(reach[d]), int(depth[d]), int(fan_in[d]),
            int(fan_out[d]), float(ppr[d]), int(idom[d]), int(dom_mass[d]), int(bridge[d]), int(aux[d]),
            float(round(score[d], 6)), int(pillar_rank[d]) or None,
        ))
    con.executemany("INSERT INTO decl_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    _write_tree(con, specs, ordering, modules, module_pillar_list, live_per_module, total_per_module, module_of, score, live)
    stats = {
        "cone_version": CONE_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "index_path": str(Path(index_path).resolve()),
        "parser_version": PARSER_VERSION,
        "decls": int(exists.sum()),
        "live": int(live.sum()),
        "dead": int((exists & ~live).sum()),
        # examples and anonymous instances have no name to match against the spine; the
        # camera-facing "dead code" share is measured over named declarations
        "named": int(sum(1 for d in range(1, n_ids) if names[d])),
        "dead_named": int(sum(1 for d in range(1, n_ids) if names[d] and not live[d])),
        "edges": int(len(src)),
        "weak_edges": int(weak.sum()),
        "live_edges": int(keep.sum()),
        "roots": json.dumps([names[r] for r in roots]),
        "liveness_source": "spine" if live_spine is not None else "graph",
        "agreement": json.dumps(agreement),
        "pillars": len(specs),
        "timeline_steps": len(items),
        "timeline_matched": sum(len(s.timeline) for s in specs),
        "flow_forward_pct": round(100 * flow.forward_frac, 2),
        "flow_method": "exact" if flow.exact else "greedy",
        "max_depth": int(depth.max()) if len(depth) else 0,
        "timing_edges_s": f"{edges_s:.1f}",
        "timing_total_s": f"{time.time() - t0:.1f}",
    }
    con.executemany("INSERT INTO cone_meta VALUES (?, ?)", [(k, str(v)) for k, v in stats.items()])
    con.executescript(POST)
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("ANALYZE")
    con.close()
    os.replace(tmp, out_path)
    log(f"cone written to {out_path} in {time.time() - t0:.1f}s")
    return stats


def _write_tree(con, specs, ordering, modules, module_pillar_list, live_pm, total_pm, module_of, score, live) -> None:
    """root -> pillars (+ pruned) -> directory nodes -> module leaves, with aggregated counts."""
    best_by_module: dict[int, tuple[float, int]] = {}
    for d in np.flatnonzero(live):
        m, s = int(module_of[d]), float(score[d])
        if m not in best_by_module or s > best_by_module[m][0]:
            best_by_module[m] = (s, int(d))

    nodes: dict[tuple, dict] = {}
    children: dict[tuple, list[tuple]] = defaultdict(list)

    def ensure(key: tuple, parent: tuple | None, kind: str, label: str, path: str, pillar, module_id=None) -> dict:
        if key not in nodes:
            nodes[key] = {"parent": parent, "kind": kind, "label": label, "path": path, "pillar": pillar,
                          "module_id": module_id, "modules": 0, "decls": 0, "live": 0, "top": (0.0, None)}
            if parent is not None:
                children[parent].append(key)
        return nodes[key]

    root = ("root",)
    ensure(root, None, "root", ROOT_MODULE, "", None)
    for p in ordering:
        ensure(("pillar", p), root, "pillar", specs[p].title, specs[p].prefixes[0], p)
    for (mid, name, _path, _first, _count), p in zip(modules, module_pillar_list, strict=True):
        pillar_key = ("pillar", p)
        if p < 0:
            ensure(pillar_key, root, "pillar", "Pruned (no live declarations)", "", -1)
            rel = name.split(".")
        else:
            prefixes = specs[p].prefixes
            matched = max((pre for pre in prefixes if name == pre or name.startswith(pre + ".")), key=len, default="")
            tail = name[len(matched) + 1 :].split(".") if matched and name != matched else ([] if matched else name.split("."))
            lead = [matched.rsplit(".", 1)[-1]] if len(prefixes) > 1 and matched else []
            rel = lead + tail
        if not rel:
            rel = [name.rsplit(".", 1)[-1]]
        parent = pillar_key
        for i in range(len(rel) - 1):
            key = (*pillar_key, "d", *rel[: i + 1])
            ensure(key, parent, "dir", pl.humanize(rel[i]), ".".join(rel[: i + 1]), p)
            parent = key
        leaf = ensure(("module", mid), parent, "module", rel[-1], name, p, mid)
        leaf["modules"] = 1
        leaf["decls"] = int(total_pm[mid])
        leaf["live"] = int(live_pm[mid])
        leaf["top"] = best_by_module.get(mid, (0.0, None))

    # bounded fan-out: wide directories get synthetic name-prefix / alphabetical-range groups
    for parent in list(children):
        kids = children[parent]
        if len(kids) <= tg.FANOUT:
            continue
        plan = tg.plan_groups([(k, nodes[k]["label"]) for k in kids], tg.FANOUT)
        children[parent] = []
        base = nodes[parent]

        def attach(entries: list, parent_key: tuple, trail: tuple, base: dict = base) -> None:
            for e in entries:
                if isinstance(e, tg.Group):
                    gkey = (*parent_key, "g", *trail, e.label)
                    ensure(gkey, parent_key, "group", e.label, base["path"], base["pillar"])
                    attach(e.members, gkey, (*trail, e.label))
                else:
                    nodes[e]["parent"] = parent_key
                    children[parent_key].append(e)

        attach(plan, parent, ())

    # breadth-first ids (parents before children), children sorted by live mass
    order: list[tuple] = []
    queue: deque[tuple] = deque([root])
    while queue:
        key = queue.popleft()
        order.append(key)
        queue.extend(sorted(children.get(key, []), key=lambda c: (-nodes[c]["live"], nodes[c]["label"])))
    for key in reversed(order):  # aggregate bottom-up
        n = nodes[key]
        parent = n["parent"]
        if parent is None:
            continue
        pn = nodes[parent]
        pn["modules"] += n["modules"]
        pn["decls"] += n["decls"]
        pn["live"] += n["live"]
        if n["top"][1] is not None and n["top"][0] > pn["top"][0]:
            pn["top"] = n["top"]
    ids = {key: i for i, key in enumerate(order)}
    rows = []
    for key in order:
        n = nodes[key]
        rows.append((
            ids[key], ids[n["parent"]] if n["parent"] is not None else None, n["kind"], n["label"], n["path"],
            n["pillar"], n["module_id"], n["modules"], n["decls"], n["live"], n["decls"] - n["live"],
            n["top"][1], round(n["top"][0], 6),
        ))
    con.executemany("INSERT INTO tree_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def cone_stats(cone_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{cone_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    meta = dict(con.execute("SELECT key, value FROM cone_meta").fetchall())
    if meta.get("cone_version") != CONE_VERSION:
        con.close()
        raise RuntimeError(f"cone was built by v{meta.get('cone_version')}, current is v{CONE_VERSION}: run `cone build` again")
    pillars_rows = [dict(r) for r in con.execute("SELECT * FROM pillars ORDER BY flow_ord")]
    con.close()
    for p in pillars_rows:
        p["timeline"] = json.loads(p["timeline"])
        p["keywords"] = json.loads(p["keywords"])
        p["prefixes"] = json.loads(p["prefixes"])
    meta["agreement"] = json.loads(meta.get("agreement") or "{}")
    meta["roots"] = json.loads(meta.get("roots") or "[]")
    return {"meta": meta, "pillars": pillars_rows}
