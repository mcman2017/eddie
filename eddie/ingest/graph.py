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


# --- declaration-level algorithms ---------------------------------------------------------------


def csr(n: int, src: np.ndarray, dst: np.ndarray) -> tuple[list[int], list[int]]:
    """CSR adjacency as Python lists (fast scalar indexing in tight loops)."""
    ptr, idx = _csr(n, np.asarray(src, dtype=np.int64), np.asarray(dst, dtype=np.int64))
    return ptr.tolist(), idx.tolist()


def immediate_dominators(n: int, src: np.ndarray, dst: np.ndarray, roots: list[int]) -> tuple[np.ndarray, list[int]]:
    """Cooper–Harvey–Kennedy iterative dominators from a virtual super-root over `roots`.

    Edges point user -> used, so a declaration d dominates v when every reference path
    from Main to v passes through d. Returns (idom, postorder):
    * idom[v] is the immediate dominator of v;
    * idom[v] == n means v is dominated only by the virtual root;
    * idom[v] == -1 means v is unreachable.
    Converges in one pass on DAGs (plus one verification pass); cycles also work.
    """
    S = n
    ptr, idx = csr(n, src, dst)
    visited = bytearray(n + 1)
    postorder: list[int] = []
    visited[S] = 1
    stack: list[list[int]] = [[S, 0]]
    while stack:
        frame = stack[-1]
        v, i = frame
        if v == S:
            if i < len(roots):
                frame[1] = i + 1
                w = roots[i]
                if not visited[w]:
                    visited[w] = 1
                    stack.append([w, ptr[w]])
                continue
        elif i < ptr[v + 1]:
            frame[1] = i + 1
            w = idx[i]
            if not visited[w]:
                visited[w] = 1
                stack.append([w, ptr[w]])
            continue
        stack.pop()
        postorder.append(v)

    po = [-1] * (n + 1)
    for pos, v in enumerate(postorder):
        po[v] = pos
    # predecessors restricted to reachable nodes; the super-root precedes every root
    rsrc = np.asarray(src, dtype=np.int64)
    rdst = np.asarray(dst, dtype=np.int64)
    reach = np.array([po[v] >= 0 for v in range(n)], dtype=bool) if n else np.zeros(0, dtype=bool)
    keep = reach[rsrc] & reach[rdst] if len(rsrc) else np.zeros(0, dtype=bool)
    pptr, pidx = csr(n, rdst[keep], rsrc[keep])
    root_set = set(roots)

    idom = [-1] * (n + 1)
    idom[S] = S

    def intersect(a: int, b: int) -> int:
        while a != b:
            while po[a] < po[b]:
                a = idom[a]
            while po[b] < po[a]:
                b = idom[b]
        return a

    rpo = postorder[::-1]
    changed = True
    while changed:
        changed = False
        for v in rpo:
            if v == S:
                continue
            new = S if v in root_set else -1
            for k in range(pptr[v], pptr[v + 1]):
                p = pidx[k]
                if idom[p] != -1:
                    new = p if new == -1 else intersect(p, new)
            if new != idom[v]:
                idom[v] = new
                changed = True
    out = np.array(idom[:n], dtype=np.int64)
    return out, postorder


def dominated_mass(n: int, idom: np.ndarray, postorder: list[int]) -> np.ndarray:
    """Size of each node's dominator subtree (itself included); 0 when unreachable."""
    size = [0] * (n + 1)
    for v in postorder:
        if v == n:
            continue
        size[v] += 1
        parent = int(idom[v])
        if parent >= 0:
            size[parent] += size[v]
    return np.array(size[:n], dtype=np.int64)


def personalized_pagerank(
    n: int, src: np.ndarray, dst: np.ndarray, roots: list[int], *, damping: float = 0.85, iters: int = 60
) -> np.ndarray:
    """PageRank whose walker restarts at the roots: 'how load-bearing is d for Main?'."""
    rank = np.zeros(n)
    if not n or not roots:
        return rank
    src = np.asarray(src, dtype=np.int64)
    dst = np.asarray(dst, dtype=np.int64)
    teleport = np.zeros(n)
    teleport[roots] = 1.0 / len(roots)
    out_deg = np.bincount(src, minlength=n).astype(np.float64) if len(src) else np.zeros(n)
    weights = np.zeros(len(src))
    if len(src):
        weights = 1.0 / out_deg[src]
    dangling = out_deg == 0
    rank = teleport.copy()
    for _ in range(iters):
        contrib = np.bincount(dst, weights=rank[src] * weights, minlength=n) if len(src) else np.zeros(n)
        new = (1 - damping) * teleport + damping * (contrib + rank[dangling].sum() * teleport)
        if np.abs(new - rank).sum() < 1e-12:
            rank = new
            break
        rank = new
    total = rank.sum()
    return rank / total if total else rank
