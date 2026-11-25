import numpy as np

from eddie.ingest.graph import compute_metrics, detect_source_root, find_project_root, module_name


def test_chain_and_diamond_metrics():
    # 0=Main imports 1 and 2; 1 and 2 import 3; 3 imports 4 (leaf); 5=Statements imports 4
    edges = [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4), (5, 4)]
    m = compute_metrics(6, edges, main_roots=[0], statements_roots=[5])
    assert m.out_degree.tolist() == [2, 1, 1, 1, 0, 1]
    assert m.in_degree.tolist() == [0, 1, 1, 2, 2, 0]
    assert m.dist_main.tolist() == [0, 1, 1, 2, 3, -1]
    assert m.dist_statements.tolist() == [-1, -1, -1, -1, 1, 0]
    assert m.height.tolist() == [3, 2, 2, 1, 0, 1]
    assert m.depth.tolist() == [0, 1, 1, 2, 3, 0]
    assert not m.in_cycle.any()
    assert abs(m.pagerank.sum() - 1.0) < 1e-9
    assert int(np.argmax(m.pagerank)) == 4  # the shared foundation collects the most rank


def test_duplicate_and_self_edges_are_ignored():
    m = compute_metrics(2, [(0, 1), (0, 1), (1, 1)], [0], [])
    assert m.out_degree.tolist() == [1, 0]
    assert m.in_degree.tolist() == [0, 1]


def test_cycles_are_flagged_not_fatal():
    edges = [(0, 1), (1, 2), (2, 1), (2, 3)]
    m = compute_metrics(4, edges, [0], [])
    assert m.in_cycle.tolist() == [False, True, True, False]
    assert m.height[1] == -1 and m.height[3] == 0
    assert m.dist_main.tolist() == [0, 1, 2, 3]


def test_empty_and_isolated():
    m = compute_metrics(3, [], [0], [])
    assert m.dist_main.tolist() == [0, -1, -1]
    assert abs(m.pagerank.sum() - 1.0) < 1e-9


def test_module_names_and_source_root(tmp_path):
    proj = tmp_path / "wrapper" / "lean"
    (proj / "src" / "A").mkdir(parents=True)
    (proj / "lakefile.toml").write_text('name = "tree"\nsrcDir = "src"\n')
    assert find_project_root(tmp_path / "wrapper") == proj
    assert detect_source_root(proj) == proj / "src"
    assert module_name("A/B₂/C.lean") == "A.B₂.C"
