"""Cone build on the synthetic tree: liveness (oracle vs graph), dominators, pillars, zoom tree."""

from __future__ import annotations

import gzip
import sqlite3

import pytest

from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone, cone_stats
from eddie.ingest.synthetic import write_sample_tree

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def cone(tmp_path_factory):
    root = tmp_path_factory.mktemp("cone")
    write_sample_tree(root / "tree", scale=3)
    index = root / "index.sqlite"
    build_index(root / "tree", index, workers=1, log=lambda *_: None)
    spine = root / "spine.tsv.gz"
    with gzip.open(spine, "wt") as fh:  # oracle: the graph cone plus the Statements theorems
        for name in ("riemannHypothesis", "riemannZeta_eq_zero_re", "Development.Spectral.Trace.critical_line_of_trace",
                     "Development.Primes.Explicit.no_off_line_zero", "Lemmata.Bounds.explicit_bound_contradiction",
                     "Statements.LFunction_eq_zero_re"):
            fh.write(f"{name}\tthm\n")
    out = root / "cone.sqlite"
    stats = build_cone(index, out, spine_path=spine, workers=2, log=lambda *_: None)
    con = sqlite3.connect(out)
    con.execute(f"ATTACH 'file:{index}?mode=ro' AS idx")
    return stats, con, out


def _metric(con, name, column):
    return con.execute(
        f"SELECT m.{column} FROM decl_metrics m JOIN idx.decls d ON d.id = m.decl_id WHERE d.full_name = ?", (name,)
    ).fetchone()[0]


def test_liveness_oracle_and_graph_agreement(cone):
    stats, con, _ = cone
    assert stats["liveness_source"] == "spine"
    assert stats["live"] == 6
    assert _metric(con, "Statements.LFunction_eq_zero_re", "live") == 1  # oracle says live
    assert _metric(con, "Statements.LFunction_eq_zero_re", "live_graph") == 0  # not reached by Main here
    assert _metric(con, "Edge.tricky_signature", "live") == 0  # dead code
    import json

    agreement = json.loads(stats["agreement"])
    assert agreement["precision"] == 1.0 and agreement["recall"] < 1.0


def test_chain_depth_dominators_and_scores(cone):
    _, con, _ = cone
    assert _metric(con, "riemannHypothesis", "depth") == 0
    assert _metric(con, "Development.Spectral.Trace.critical_line_of_trace", "depth") == 1
    assert _metric(con, "Lemmata.Bounds.explicit_bound_contradiction", "depth") == 3
    rh_id = con.execute("SELECT id FROM idx.decls WHERE full_name='riemannHypothesis'").fetchone()[0]
    assert _metric(con, "Development.Spectral.Trace.critical_line_of_trace", "idom") == rh_id
    assert _metric(con, "riemannHypothesis", "dom_mass") == 4
    assert _metric(con, "riemannHypothesis", "score") == 1.0
    assert _metric(con, "Edge.tricky_signature", "score") == 0.0


def test_pillars_and_zoom_tree(cone):
    _, con, out = cone
    titles = [r[0] for r in con.execute("SELECT title FROM pillars ORDER BY ord")]
    assert titles[0] == "Top-level reduction"
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM tree_nodes GROUP BY kind").fetchall())
    assert kinds["root"] == 1 and kinds["module"] == 14
    root = con.execute("SELECT id, live, decls FROM tree_nodes WHERE kind = 'root'").fetchone()
    assert root[1] == 6 and root[2] > root[1]
    # every module leaf hangs under exactly one pillar
    orphans = con.execute(
        "SELECT COUNT(*) FROM tree_nodes t WHERE t.kind = 'module' AND t.parent_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0
    st = cone_stats(out)
    assert st["meta"]["roots"] == ["riemannHypothesis", "riemannZeta_eq_zero_re"]


def test_parser_version_mismatch_is_refused(tmp_path):
    write_sample_tree(tmp_path / "tree")
    index = tmp_path / "index.sqlite"
    build_index(tmp_path / "tree", index, workers=1, log=lambda *_: None)
    con = sqlite3.connect(index)
    con.execute("UPDATE index_meta SET value = '0' WHERE key = 'parser_version'")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="rebuild the index"):
        build_cone(index, tmp_path / "cone.sqlite", workers=1, log=lambda *_: None)
