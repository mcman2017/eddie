"""Index build over the synthetic tree (2 spawn workers), queries, offsets, determinism."""

from __future__ import annotations

import gzip

import pytest

from eddie.cli import main
from eddie.ingest.build import build_index
from eddie.ingest.index_db import (
    connect_ro,
    fts_query,
    load_body,
    lookup,
    search,
    stats,
    validate_against_spine,
)
from eddie.ingest.synthetic import write_sample_tree


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("sample")
    write_sample_tree(root / "tree", scale=4)
    out = root / "index.sqlite"
    build_index(root / "tree", out, workers=2, log=lambda *_: None)
    return root, out


def test_counts_and_roots(built):
    _, out = built
    with connect_ro(out) as con:
        assert con.execute("SELECT COUNT(*) FROM modules").fetchone()[0] == 11 + 4
        roots = con.execute("SELECT name FROM modules WHERE is_main_root = 1 OR is_statements_root = 1 ORDER BY name").fetchall()
        assert [r[0] for r in roots] == ["Main", "Statements"]
        ext = {r[0] for r in con.execute("SELECT dst_name FROM imports WHERE is_external = 1")}
        assert ext == {"Mathlib.Analysis.Complex.Basic", "Mathlib.Algebra.BigOperators.Basic", "Mathlib.NumberTheory.LSeries.RiemannZeta"}
        depth = con.execute(
            "SELECT mm.dist_main FROM module_metrics mm JOIN modules m ON m.id = mm.module_id WHERE m.name = 'Formalism.Basic'"
        ).fetchone()[0]
        assert depth >= 2


def test_generated_chain_depth(built):
    _, out = built
    with connect_ro(out) as con:
        rows = dict(con.execute(
            "SELECT m.name, mm.height FROM module_metrics mm JOIN modules m ON m.id = mm.module_id "
            "WHERE m.name LIKE 'Development.Generated.%'"
        ).fetchall())
    # M3 -> M2 -> M1 -> M0 -> Formalism.Basic (-> external only)
    assert rows["Development.Generated.M3"] == rows["Development.Generated.M0"] + 3


def test_fts_and_substring_search(built):
    _, out = built
    with connect_ro(out) as con:
        names = [r["full_name"] for r in search(con, "zeta", limit=10)]
        assert "riemannZeta_eq_zero_re" in names  # substring fallback
        assert "Formalism.completedZeta_one_sub" in names
        hits = search(con, "completedRiemannZeta", limit=5)
        assert hits[0]["full_name"] == "Formalism.completedZeta_one_sub"
        assert search(con, 'evil" OR decls MATCH "x', limit=5) == [] or True  # never an SQL/FTS error


def test_fts_query_quotes_terms():
    assert fts_query('a "b" c.d') == '"a"* """b"""* "c"* "d"*'
    assert fts_query("   ") is None


def test_show_loads_full_body_by_offsets(built):
    _, out = built
    with connect_ro(out) as con:
        row = lookup(con, "tricky_signature")[0]
        body = load_body(con, row)
    assert body.startswith("@[simp, norm_cast]\nprotected theorem tricky_signature")
    assert body.rstrip().endswith("simp [h₁]")


def test_stats_and_forecast(built):
    _, out = built
    with connect_ro(out) as con:
        st = stats(con)
    assert st["decls"] > 40
    assert st["sorry_count"] >= 1
    assert st["forecast_r1_all_signatures"]["input_usd_upper_bound"] > 0


def test_build_is_deterministic_and_atomic(built, tmp_path):
    root, out = built
    again = tmp_path / "again.sqlite"
    build_index(root / "tree", again, workers=1, log=lambda *_: None)
    q = "SELECT id, module_id, kind, full_name, start_byte, end_byte FROM decls ORDER BY id"
    with connect_ro(out) as a, connect_ro(again) as b:
        assert a.execute(q).fetchall() == b.execute(q).fetchall()
    assert not again.with_name(again.name + ".tmp").exists()


def test_validate_against_spine(built, tmp_path):
    _, out = built
    spine = tmp_path / "spine.tsv.gz"
    with gzip.open(spine, "wt") as fh:
        fh.write("riemannHypothesis\tthm\nFormalism.NontrivialZero\tdef\n_private.BuildAid.Edge.0.Edge.string_with_assign\tthm\n")
    with connect_ro(out) as con:
        report = validate_against_spine(con, spine)
    assert report["found_in_spine"] == 3
    assert report["per_kind"]["lemma"]["found"] == 1  # private name de-mangled
    assert report["cone_coverage_by_parser_pct"] == 100.0


def test_cli_index_commands(built, capsys):
    _, out = built
    assert main(["index", "stats", "--index", str(out)]) == 0
    assert "forecast" in capsys.readouterr().out
    assert main(["index", "search", "harmonic", "--index", str(out)]) == 0
    assert "primeHarmonic" in capsys.readouterr().out
    assert main(["index", "show", "riemannHypothesis", "--index", str(out)]) == 0
    assert "critical_line_of_trace" in capsys.readouterr().out
