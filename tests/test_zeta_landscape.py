"""Landscape precomputation: zeta, the signature code, the Dirichlet wave, zero finding, statistics."""

from __future__ import annotations

import gzip
import json
import math
import sqlite3

import numpy as np
import pytest
from scipy import ndimage

from eddie.planner import zeta_landscape as zl

# --- zeta -----------------------------------------------------------------------------------------


def test_zeta_grid_matches_known_values_and_reports_the_pole():
    z, pole = zl.zeta_grid(np.array([0.0, 0.5, 1.0]), np.array([0.0, 1.0]))
    assert pole == [2, 0] and np.isnan(z[2, 0])
    assert z[0, 0] == pytest.approx(-0.5, rel=1e-12)
    assert z[1, 0] == pytest.approx(-1.4603545088095868, rel=1e-12)
    assert z[1, 1] == pytest.approx(0.14393642707718907 - 0.722099743531673j, rel=1e-12)
    assert z[2, 1] == pytest.approx(0.5821580597520036 - 0.9268485643308071j, rel=1e-12)


def test_zeta_zeros_are_listed_up_to_the_top_of_the_box():
    zeros = zl.zeta_zeros(30.0)
    assert [z["t"] for z in zeros] == pytest.approx([14.134725141734693, 21.022039638771555, 25.010857580145688], abs=1e-9)
    assert all(z["slope"] > 0 for z in zeros)
    assert zl.zeta_zeros(14.0) == [] and len(zl.zeta_zeros(14.2)) == 1


def test_grid_satisfies_the_functional_equation():
    sigmas, ts = np.linspace(0, 1, 5), np.linspace(0, 20, 9)
    z, _ = zl.zeta_grid(sigmas, ts)
    assert zl.functional_equation_error(z, sigmas, ts) < 1e-9
    wrong = z.copy()
    wrong[1, 3] *= 1.01
    assert zl.functional_equation_error(wrong, sigmas, ts) > 1e-3


def test_zeta_zeros_show_up_as_unit_windings_on_the_grid():
    sigmas, ts = np.linspace(0.3, 0.7, 5), np.linspace(13.0, 15.0, 17)
    z, _ = zl.zeta_grid(sigmas, ts)
    turns = zl.winding(z)
    assert turns.sum() == 1 and (turns >= 0).all()
    i, j = np.argwhere(turns == 1)[0]
    assert ts[j] <= 14.134725 <= ts[j + 1] and sigmas[i] <= 0.5 <= sigmas[i + 1]


# --- the signature code -----------------------------------------------------------------------------


def test_bucket_is_an_exact_integer_log2():
    assert zl.bucket([0, 1, 2, 3, 6, 7]).tolist() == [0, 1, 1, 2, 2, 3]
    ks = np.arange(1, 50)
    assert zl.bucket(2**ks - 1).tolist() == ks.tolist()
    assert zl.bucket(2**ks - 2).tolist() == (ks - 1).tolist()
    assert zl.bucket([-1]).tolist() == [0]  # a height of -1 (import cycle) is clamped


def test_signatures_code_and_decode():
    sig = zl.signatures(height=[3, 0, 0, 9, 3], fan_in=[0, 0, 0, 0, 0], fan_out=[3, 0, 0, 0, 3], rank=[0, 2, -1, 1, 0],
                        live=[4, 1, 0, 0, 2], dead=[0, 2, 3, 0, 1], imports=[[0, 1], [4, 1], [4, 1], [0, 4]], n_pillars=3)
    codes = dict(zip(zl.godel_integers(sig), zip(sig.live.tolist(), sig.dead.tolist(), sig.modules.tolist(), strict=True), strict=True))
    # modules 0 and 4 share a tuple: 2^2 * 5^2 * 7^2 (own, rank 0) * 13 (imports rank 2) = 63,700
    assert codes[63_700] == (6.0, 1.0, 2)
    assert codes[13**2] == (1.0, 2.0, 1)
    assert codes[17**2] == (0.0, 3.0, 1)  # no pillar: the reserved prime, so l stays positive
    assert len(codes) == 3  # the module without declarations defines no class
    ln_n = sig.exponents @ zl.log_primes(3)
    assert np.exp(ln_n) == pytest.approx(sorted(codes), rel=1e-12) or sorted(np.exp(ln_n)) == pytest.approx(sorted(codes), rel=1e-12)
    assert sorted(sig.own.tolist()) == [-1, 0, 2]


def test_godel_integers_are_exact_beyond_64_bits():
    sig = zl.signatures(height=[2**40], fan_in=[2**30], fan_out=[2**20], rank=[5], live=[1], dead=[0],
                        imports=np.zeros((0, 2), dtype=int), n_pillars=6)
    (n,) = zl.godel_integers(sig)
    assert n == 2**40 * 3**30 * 5**20 * 23**2 and n > 2**63


def test_other_conventions_change_l_but_not_the_classes():
    assert zl.log_primes(3).tolist() == pytest.approx(np.log([2, 3, 5, 7, 11, 13, 17]).tolist())
    assert zl.log_primes(3, offset=1).tolist() == pytest.approx(np.log([2, 3, 5, 11, 13, 17, 19]).tolist())
    assert zl.log_primes(3, perm=[2, 0, 1]).tolist() == pytest.approx(np.log([2, 3, 5, 13, 7, 11, 17]).tolist())


# --- the series -------------------------------------------------------------------------------------


def series(n: int = 60, seed: int = 5) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    w = rng.random(n)
    return rng.uniform(0.4, 4.0, n), w / w.sum()


def test_normalisation_symmetry_and_bounds():
    ell, w = series()
    sigmas, ts = np.array([0.0, 0.5, 1.0]), np.array([-3.0, 0.0, 3.0])
    z = zl.evaluate(ell, w, sigmas, ts)[0]
    assert z[0, 1] == pytest.approx(1.0, abs=1e-12)  # Z(0) = 1
    assert z[:, 0] == pytest.approx(np.conj(z[:, 2]), abs=1e-12)  # Z(conj s) = conj Z(s)
    assert (np.abs(z) <= z[:, [1]].real + 1e-12).all() and (z[:, 1].real <= 1 + 1e-12).all()
    assert zl.dirichlet(ell, w, 0.5 + 3j)[0] == pytest.approx(z[1, 2], abs=1e-12)


def test_bands_partition_the_classes_and_sum_to_the_whole():
    ell, w = series()
    band, edges = zl.band_index(ell, w, 4)
    assert sorted(set(band.tolist())) == [0, 1, 2, 3] and len(edges) == 3
    masses = [w[band == k].sum() for k in range(4)]
    assert all(0.15 < m < 0.35 for m in masses)
    sigmas, ts = np.linspace(0, 1, 5), np.linspace(0, 20, 41)
    parts = zl.evaluate(ell, np.stack([w * (band == k) for k in range(4)]), sigmas, ts)
    assert parts.sum(axis=0) == pytest.approx(zl.evaluate(ell, w, sigmas, ts)[0], abs=1e-12)
    assert zl.band_index(ell, w, 200)[0].max() < 200  # more bands than classes leaves some empty


def test_two_term_series_has_its_zeros_on_the_line():
    ell = np.array([1.0, 2.0])
    w = np.array([1.0, math.exp(0.5)])
    w /= w.sum()
    (roots,), info = zl.find_zeros(ell, w, 60.0, base_ds=1 / 16, base_dt=0.25)
    assert info["negative"] == 0 and len(roots) == 10
    for k, r in enumerate(roots):
        assert r["refined"] and r["charge"] == 1
        assert r["sigma"] == pytest.approx(0.5, abs=1e-10) and r["t"] == pytest.approx((2 * k + 1) * math.pi, abs=1e-10)


def test_a_single_class_has_no_zeros_and_no_structure():
    ell, w = np.array([1.3]), np.array([1.0])
    (roots,), _ = zl.find_zeros(ell, w, 30.0, base_ds=1 / 8, base_dt=0.5)
    stats = zl.mass_stats(ell, w)
    assert roots == [] and stats["n1"] == pytest.approx(1.0) and stats["n2"] == pytest.approx(1.0)
    assert stats["r_inf_half"] == pytest.approx(1.0)
    assert zl.mass_stats(ell, np.array([0.0])) == {"present": False}
    assert zl.frequency_scale(ell, w, np.linspace(0, 1, 9), 30.0, 0.5, 3)["kappa"] == 1.0


def test_winding_counts_zeros_and_poles_with_multiplicity():
    sigmas, ts = np.linspace(0, 1, 81), np.linspace(0, 1, 81)
    s = sigmas[:, None] + 1j * ts[None, :]
    f = (s - (0.305 + 0.295j)) * (s - (0.705 + 0.605j)) ** 2 / (s - (0.405 + 0.805j))  # all off the grid nodes
    turns = zl.winding(f)
    labels, n = ndimage.label(turns != 0, structure=np.ones((3, 3), dtype=int))
    assert sorted(int(turns[labels == k].sum()) for k in range(1, n + 1)) == [-1, 1, 2]


def test_scale_rule_hits_its_target_and_statistics_are_exact():
    ell, w = series(200)
    sigmas = np.linspace(0, 1, 41)
    scale = zl.frequency_scale(ell, w, sigmas, 60.0, 0.125, 13)
    unit = float(w @ ell)
    assert scale["unit"] == pytest.approx(unit)
    assert zl.expected_zeros(scale["kappa"] * ell / unit, w, sigmas, 60.0) == pytest.approx(13.0, abs=1e-6)
    even = np.full(25, 1 / 25)
    stats = zl.mass_stats(np.linspace(1, 2, 25), even)
    assert stats["n1"] == pytest.approx(25) and stats["n2"] == pytest.approx(25)
    assert zl.mass_stats(np.ones(25), even)["r_inf_0"] == pytest.approx(1 / 5)  # 1 / sqrt(N2)


def test_cancellation_budget_adds_up():
    ell, w = np.array([1.0, 2.0]), np.array([0.5, 0.5])
    b = zl.cancellation_budget(ell, w, 0.5 + 1j * math.pi, np.array([0, 1]), 2)
    assert b["within"] == pytest.approx(0.0, abs=1e-12)
    assert b["residual"] == pytest.approx(math.tanh(0.25), abs=1e-12)
    assert b["within"] + b["between"] + b["residual"] == pytest.approx(1.0)
    assert {g["side"] for g in b["groups"]} == {1, -1} and b["opposition"] > 0.7
    merged = zl.cancellation_budget(ell, w, 0.5 + 1j * math.pi, np.array([0, 0]), 1)
    assert merged["between"] == pytest.approx(0.0, abs=1e-12) and merged["within"] == pytest.approx(1 - math.tanh(0.25))


def test_alignment_detects_a_comb_in_the_dips_and_ignores_one_on_the_crests():
    ts = np.linspace(0, 60, 6001)
    wave = np.cos(ts)  # dips at odd multiples of pi
    dips = [(2 * k + 1) * math.pi for k in range(2, 9)]
    crests = [2 * k * math.pi for k in range(2, 9)]
    assert zl.alignment(ts, wave, dips, 60.0)["p"] < 0.01
    assert zl.alignment(ts, wave, crests, 60.0)["p"] > 0.99
    assert zl.alignment(ts, wave, dips[:2], 60.0) == {"available": False}


def test_seeded_draws_repeat():
    assert zl.uniforms(7, 5).tolist() == zl.uniforms(7, 5).tolist()
    assert zl.uniforms(7, 5).tolist() != zl.uniforms(8, 5).tolist()
    assert ((zl.uniforms(7, 1000) >= 0) & (zl.uniforms(7, 1000) < 1)).all()


def test_fft_window_statistics_agree_with_the_exact_path():
    ell = np.array([1.0, 2.0])
    w = np.array([1.0, math.exp(0.5)])
    w /= w.sum()
    plan = zl.WindowStats(60.0, 0.0625)
    assert plan.phase_error <= 0.05
    exact = zl.evaluate(ell, w, plan.sigmas, plan.ts)[0]
    assert np.abs(plan.field(ell, w.astype(complex)) - exact).max() < 0.05
    stats = plan.stats(ell, w)
    assert stats["zeros"] in (9, 10, 11) and stats["near_line"] == stats["zeros"]  # zeros sit on a cell edge
    assert plan.stats(ell, np.zeros(2)) == {"zeros": 0, "near_line": 0, "sharp": 0, "rms": 0.0}


def test_layout_runs_from_the_wings_to_main():
    chapters = [{"id": 3, "tier": "foundations", "live": 50, "dead": 9}, {"id": 1, "tier": "machinery", "live": 30, "dead": 0},
                {"id": 2, "tier": "bottleneck", "live": 20, "dead": 4}, {"id": 0, "tier": "main", "live": 2, "dead": 0}]
    weights = np.zeros((4, 4), dtype=int)
    weights[1, 3], weights[2, 1], weights[0, 2] = 40, 25, 3  # 1 builds on 3, 2 on 1, the core on 2
    layout, arcs = zl.strip_layout(chapters, weights, 60.0, 1)
    ts = [c["t"] for c in layout]
    wings = [abs(c["sigma"] - 0.5) for c in layout]
    assert ts == sorted(ts) and ts[-1] == 60.0 and layout[-1]["sigma"] == 0.5 and layout[-1]["side"] == 0
    assert wings[:3] == sorted(wings[:3], reverse=True) and min(wings[:3]) >= 0.06
    assert {(a["from"], a["to"]) for a in arcs} == {(3, 1), (1, 2), (2, 0)}  # every chapter and the final flow are kept
    assert next(a for a in arcs if a["to"] == 0)["final"]
    field = zl.apron_field(layout, np.linspace(-0.3, 1.3, 33), np.linspace(0, 60, 31), 60.0)
    assert field.max() == pytest.approx(1.0) and field.min() >= 0
    assert field[(np.linspace(-0.3, 1.3, 33) > 0.1) & (np.linspace(-0.3, 1.3, 33) < 0.9)].max() < 1e-6  # nothing inside the strip


# --- end to end on the synthetic tree ---------------------------------------------------------------


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from eddie.ingest.build import build_index
    from eddie.ingest.cone import build_cone
    from eddie.ingest.synthetic import write_sample_tree

    root = tmp_path_factory.mktemp("landscape")
    write_sample_tree(root / "tree", scale=3)
    index = root / "index.sqlite"
    build_index(root / "tree", index, workers=1, log=lambda *_: None)
    con = sqlite3.connect(index)
    names = [r[0] for r in con.execute("SELECT full_name FROM decls WHERE full_name IS NOT NULL")]
    con.close()
    spine = root / "spine.tsv.gz"
    with gzip.open(spine, "wt") as fh:
        fh.writelines(f"{n}\tthm\n" for n in names if not n.startswith("Edge."))
    cone = root / "cone.sqlite"
    build_cone(index, cone, spine_path=spine, workers=1, log=lambda *_: None)
    out = root / "landscape"
    report = zl.build_landscape(index, cone, out, zl.Params.quick(), log=lambda *_: None)
    return index, cone, out, report


@pytest.mark.slow
def test_build_writes_a_consistent_asset(built):
    index, cone, out, report = built
    land = json.loads((out / "landscape.json").read_text())
    blob = (out / "fields.f32").read_bytes()
    data = np.frombuffer(blob, dtype="<f4")
    table = land["fields"]
    assert sum(int(np.prod(b["shape"])) for b in table.values()) == data.size == report["bytes"] // 4
    n_s, n_t = land["grid"]["sigma"]["n"], land["grid"]["t"]["n"]
    assert table["zeta"]["shape"] == [2, n_t, n_s] and table["proof"]["shape"] == [2, 4, 2, n_t, n_s]
    assert np.isfinite(data).all()

    def block(name):
        b = table[name]
        return data[b["offset"] : b["offset"] + int(np.prod(b["shape"]))].reshape(b["shape"])

    # zeta at sigma = 1/2, t = 0 and Z_live(0) = 1 after summing the bands
    zeta = block("zeta")
    assert zeta[0, 0, n_s // 2] == pytest.approx(-1.4603545, abs=1e-6)
    proof = block("proof")
    assert proof[0, :, 0, 0, 0].sum() == pytest.approx(1.0, abs=1e-5) and proof[0, :, 1, 0, 0].sum() == pytest.approx(0.0, abs=1e-6)
    live = land["proof"]["live"]
    assert live["mass"] == land["counts"]["live"] > 0 and sum(live["band_mass"]) == pytest.approx(1.0, abs=1e-5)
    assert len(live["zeros_by_band"]) == 4 and land["proof"]["l_min"] > 0
    assert [z["t"] for z in land["zeta"]["zeros"]] == pytest.approx([14.134725, 21.02204, 25.010858], abs=1e-6)
    assert land["zeta"]["functional_equation_error"] < 1e-9 and land["zeta"]["pole"] == [n_s - 1, 0]
    # chapters follow the flow and end at Main, on the line at the top of the box
    tiers = [c["tier"] for c in land["chapters"]]
    assert tiers[-1] == "main" and land["chapters"][-1]["sigma"] == 0.5 and land["chapters"][-1]["t"] == land["grid"]["t"]["max"]
    assert [c["chapter"] for c in land["chapters"]] == list(range(1, len(tiers) + 1))
    assert all(a["from"] != a["to"] for a in land["arcs"])
    findings = json.loads((out / "findings.json").read_text())
    assert findings["build_id"] == land["build_id"] == report["build_id"]
    assert {f["status"] for f in findings["findings"]} <= {"exact", "stable", "encoding-specific", "null"}
    assert "index_path" not in json.dumps(land) and str(index.parent) not in json.dumps(land)  # no local paths are served
    with gzip.open(out / "classes.tsv.gz", "rt") as fh:
        rows = fh.read().splitlines()
    assert rows[0].startswith("ln_n\tgodel_n") and len(rows) - 1 == land["proof"]["classes"]


@pytest.mark.slow
def test_build_is_deterministic_and_leaves_the_databases_alone(built):
    index, cone, out, report = built
    before = (index.stat().st_mtime_ns, cone.stat().st_mtime_ns)
    again = zl.build_landscape(index, cone, out.with_name("again"), zl.Params.quick(), log=lambda *_: None)
    assert again["build_id"] == report["build_id"]
    assert (out / "fields.f32").read_bytes() == (out.with_name("again") / "fields.f32").read_bytes()
    assert (out / "findings.json").read_bytes() == (out.with_name("again") / "findings.json").read_bytes()
    assert (index.stat().st_mtime_ns, cone.stat().st_mtime_ns) == before


def test_an_old_cone_is_refused(tmp_path):
    cone = tmp_path / "cone.sqlite"
    con = sqlite3.connect(cone)
    con.executescript("CREATE TABLE cone_meta (key TEXT, value TEXT); INSERT INTO cone_meta VALUES ('cone_version', '1');")
    con.commit()
    con.close()
    index = tmp_path / "index.sqlite"
    sqlite3.connect(index).close()
    with pytest.raises(RuntimeError, match="cone build"):
        zl.load(index, cone)
