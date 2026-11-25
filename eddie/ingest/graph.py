"""Module import graph: name resolution and DAG metrics.

Edges point importer -> imported. Metrics per module:

* out_degree / in_degree: internal imports / internal importers
* dist_main / dist_statements: BFS hops from the `Main` / `Statements` roots (-1 = unreachable)
* depth: longest import chain from a root down to the module (-1 = unreachable or in a cycle)
* height: longest import chain from the module down to a leaf (-1 = in a cycle)
* pagerank: importance flowing from importers to the foundations they build on
* in_cycle: the module sits on an import cycle (Lean forbids these; flagged, never fatal)
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_SRC_DIR = re.compile(r"""srcDir\s*:?=\s*["']([^"']+)["']""")


def find_project_root(path: Path) -> Path:
    """The directory holding the lakefile (descending through single wrapper dirs)."""
    path = Path(path)
    for _ in range(4):
        if any((path / name).exists() for name in ("lakefile.toml", "lakefile.lean", "lean-toolchain")):
            return path
        subdirs = [p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")]
        if len(subdirs) != 1:
            break
        path = subdirs[0]
    return path


def detect_source_root(project_root: Path) -> Path:
    for name in ("lakefile.toml", "lakefile.lean"):
        lakefile = project_root / name
        if lakefile.exists():
            m = _SRC_DIR.search(lakefile.read_text(errors="replace"))
            if m and (project_root / m.group(1)).is_dir():
                return project_root / m.group(1)
    return project_root / "src" if (project_root / "src").is_dir() else project_root


def module_name(rel_path: str) -> str:
    p = Path(rel_path)
    return ".".join(p.with_suffix("").parts)


@dataclass
class Metrics:
    in_degree: np.ndarray
    out_degree: np.ndarray
    dist_main: np.ndarray
    dist_statements: np.ndarray
    depth: np.ndarray
    height: np.ndarray
    pagerank: np.ndarray
    in_cycle: np.ndarray


def _csr(n: int, src: np.ndarray, dst: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(src, kind="stable")
    indptr = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(np.bincount(src, minlength=n), out=indptr[1:])
    return indptr, dst[order]


def _bfs(n: int, indptr: np.ndarray, targets: np.ndarray, roots: list[int]) -> np.ndarray:
    dist = np.full(n, -1, dtype=np.int64)
    queue = deque()
    for r in roots:
        dist[r] = 0
        queue.append(r)
    ip, tg = indptr.tolist(), targets.tolist()
    d = dist.tolist()
    while queue:
        v = queue.popleft()
        for k in range(ip[v], ip[v + 1]):
            w = tg[k]
            if d[w] < 0:
                d[w] = d[v] + 1
                queue.append(w)
    return np.array(d, dtype=np.int64)


def _cycle_members(n: int, ip: list[int], tg: list[int]) -> np.ndarray:
    """Iterative Tarjan SCC: True for modules on an import cycle."""
    index = [-1] * n
    low = [0] * n
    on_stack = [False] * n
    stack: list[int] = []
    member = np.zeros(n, dtype=bool)
    counter = 0
    for root in range(n):
        if index[root] >= 0:
            continue
        work = [(root, ip[root])]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            v, k = work[-1]
            if k < ip[v + 1]:
                work[-1] = (v, k + 1)
                w = tg[k]
                if index[w] < 0:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, ip[w]))
                elif on_stack[w]:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    component.append(w)
                    if w == v:
                        break
                if len(component) > 1:
                    member[component] = True
    return member


def compute_metrics(
    n: int, edges: list[tuple[int, int]], main_roots: list[int], statements_roots: list[int], *, damping: float = 0.85
) -> Metrics:
    uniq = sorted({(a, b) for a, b in edges if a != b})
    src = np.array([a for a, _ in uniq], dtype=np.int64)
    dst = np.array([b for _, b in uniq], dtype=np.int64)
    out_deg = np.bincount(src, minlength=n).astype(np.int64) if len(src) else np.zeros(n, dtype=np.int64)
    in_deg = np.bincount(dst, minlength=n).astype(np.int64) if len(dst) else np.zeros(n, dtype=np.int64)
    fwd_ptr, fwd = _csr(n, src, dst)

    dist_main = _bfs(n, fwd_ptr, fwd, main_roots)
    dist_stmt = _bfs(n, fwd_ptr, fwd, statements_roots)

    ip, tg = fwd_ptr.tolist(), fwd.tolist()
    in_cycle = _cycle_members(n, ip, tg)

    # Forward Kahn (importer -> imported): importers before what they import.
    indeg = in_deg.tolist()
    order: list[int] = [v for v in range(n) if indeg[v] == 0]
    head = 0
    while head < len(order):
        v = order[head]
        head += 1
        for k in range(ip[v], ip[v + 1]):
            w = tg[k]
            indeg[w] -= 1
            if indeg[w] == 0:
                order.append(w)

    # height: longest path to a leaf, leaves first (reverse Kahn); cycle-blocked nodes stay -1
    rev_ptr, rev = _csr(n, dst, src)
    rp, rt = rev_ptr.tolist(), rev.tolist()
    remaining = out_deg.tolist()
    height = [-1] * n
    ready = [v for v in range(n) if remaining[v] == 0]
    for v in ready:
        height[v] = 0
    head = 0
    while head < len(ready):
        v = ready[head]
        head += 1
        for k in range(rp[v], rp[v + 1]):
            u = rt[k]  # u imports v
            if height[v] + 1 > height[u]:
                height[u] = height[v] + 1
            remaining[u] -= 1
            if remaining[u] == 0:
                ready.append(u)
    for v in range(n):
        if remaining[v] > 0:
            height[v] = -1

    # depth: longest path from a root, among reachable acyclic modules
    depth = [-1] * n
    for r in set(main_roots) | set(statements_roots):
        if not in_cycle[r]:
            depth[r] = 0
    for v in order:
        if depth[v] < 0:
            continue
        for k in range(ip[v], ip[v + 1]):
            w = tg[k]
            if not in_cycle[w] and depth[v] + 1 > depth[w]:
                depth[w] = depth[v] + 1

    # PageRank (power iteration); dangling mass spread uniformly
    rank = np.full(n, 1.0 / n) if n else np.zeros(0)
    if n:
        weights = np.zeros(len(src))
        nonzero = out_deg[src] > 0
        weights[nonzero] = 1.0 / out_deg[src][nonzero]
        dangling = out_deg == 0
        for _ in range(100):
            contrib = np.bincount(dst, weights=rank[src] * weights, minlength=n)
            new = (1 - damping) / n + damping * (contrib + rank[dangling].sum() / n)
            if np.abs(new - rank).sum() < 1e-12:
                rank = new
                break
            rank = new
        rank = rank / rank.sum()

    return Metrics(
        in_degree=in_deg,
        out_degree=out_deg,
        dist_main=dist_main,
        dist_statements=dist_stmt,
        depth=np.array(depth, dtype=np.int64),
        height=np.array(height, dtype=np.int64),
        pagerank=rank,
        in_cycle=in_cycle,
    )
