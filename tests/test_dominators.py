import numpy as np

from eddie.ingest.graph import dominated_mass, immediate_dominators, personalized_pagerank


def _dom(n, edges, roots):
    src = np.array([a for a, _ in edges], dtype=np.int64)
    dst = np.array([b for _, b in edges], dtype=np.int64)
    idom, post = immediate_dominators(n, src, dst, roots)
    return idom.tolist(), dominated_mass(n, np.array(idom), post).tolist()


def test_diamond_join_is_dominated_by_the_root():
    # 0 -> 1, 0 -> 2, 1 -> 3, 2 -> 3, 3 -> 4
    idom, mass = _dom(5, [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4)], [0])
    assert idom == [5, 0, 0, 0, 3]  # 5 = virtual super-root
    assert mass == [5, 1, 1, 2, 1]


def test_chain_bottleneck_dominates_everything_below():
    idom, mass = _dom(4, [(0, 1), (1, 2), (1, 3)], [0])
    assert idom == [4, 0, 1, 1]
    assert mass[1] == 3


def test_cycles_and_unreachable_nodes():
    idom, mass = _dom(5, [(0, 1), (1, 2), (2, 1), (2, 3)], [0])
    assert idom[:4] == [5, 0, 1, 2]
    assert idom[4] == -1 and mass[4] == 0


def test_multiple_roots_meet_at_the_super_root():
    idom, _ = _dom(3, [(0, 2), (1, 2)], [0, 1])
    assert idom == [3, 3, 3]


def test_personalized_pagerank_restarts_at_roots():
    src = np.array([0, 0, 1, 2], dtype=np.int64)
    dst = np.array([1, 2, 3, 3], dtype=np.int64)
    r = personalized_pagerank(5, src, dst, [0])
    assert abs(r.sum() - 1) < 1e-9
    assert r[4] == 0  # unreachable from the root
    assert r[0] > r[1] and r[3] > r[1]  # the shared foundation collects mass
