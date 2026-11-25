"""seed-demo: sandbox witnesses attached to Main's theorems, all labeled seed, idempotent, $0."""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
import wave

import mpmath
import numpy as np
import pytest
from matplotlib import image as mpimg

from eddie.demo.seed import SEED_PREFIX, SeedParams, Target, pick_targets, seed_demo
from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone
from eddie.ingest.synthetic import write_sample_tree
from eddie.planner.chapters import STUB_BADGE
from eddie.state.artifacts import ArtifactStore, connect_ro

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def cone(tmp_path_factory):
    root = tmp_path_factory.mktemp("seed")
    write_sample_tree(root / "tree", scale=2)
    index = root / "index.sqlite"
    build_index(root / "tree", index, workers=1, log=lambda *_: None)
    con = sqlite3.connect(index)
    names = [r[0] for r in con.execute("SELECT full_name FROM decls WHERE full_name IS NOT NULL")]
    con.close()
    spine = root / "spine.tsv.gz"
    with gzip.open(spine, "wt") as fh:
        fh.writelines(f"{n}\tthm\n" for n in names if not n.startswith("Edge."))
    cone_path = root / "cone.sqlite"
    build_cone(index, cone_path, spine_path=spine, workers=1, log=lambda *_: None)
    return index, cone_path, root


def run(cone, tmp_path):
    index, cone_path, root = cone
    return seed_demo(
        cone_path=cone_path, index_path=index, db_path=tmp_path / "state" / "art.sqlite",
        artifact_dir=tmp_path / "artifacts", params=SeedParams.quick(), tmp_root=tmp_path / "jobs",
        mpl_cache=root / "mplcache", log=lambda *_: None,
    )


def test_targets_follow_the_signatures_not_the_order():
    roots = [
        Target(1, "a", "theorem a : DirichletLZerosOnLine"),
        Target(2, "b", "theorem b : RiemannZetaZerosOnLine"),
        Target(3, "c", "theorem c : RiemannHypothesis"),
    ]
    t = pick_targets(roots)
    assert (t["rh"].full_name, t["strip"].full_name, t["lfun"].full_name) == ("c", "b", "a")
    only = pick_targets([Target(9, "z", "theorem z : Foo")])
    assert only["rh"].full_name == only["strip"].full_name == only["lfun"].full_name == "z"


def test_seed_writes_labeled_witnesses_and_is_idempotent(cone, tmp_path):
    first = run(cone, tmp_path)
    db = tmp_path / "state" / "art.sqlite"
    ArtifactStore(db).put_triage("Some.mock.decl", "a mock gloss", source="mock")
    second = run(cone, tmp_path)
    assert [w["kind"] for w in first["witnesses"]] == [w["kind"] for w in second["witnesses"]]
    with connect_ro(db) as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM witnesses ORDER BY id")]
        intuitions = [dict(r) for r in con.execute("SELECT * FROM intuitions")]
        reviews = [dict(r) for r in con.execute("SELECT * FROM reviews")]
        activity = dict(con.execute("SELECT * FROM activity").fetchone())
        mock = con.execute("SELECT COUNT(*) FROM triage WHERE source = 'mock'").fetchone()[0]
    assert len(rows) == 6 and {r["source"] for r in rows} == {"seed"}
    assert mock == 1  # re-seeding only replaces seed rows
    kinds = [(r["kind"], r["status"]) for r in rows]
    assert kinds.count(("plot", "verified")) == 3 and ("plot", "rejected") in kinds
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["spectrogram"]["pair_id"] == by_kind["audio"]["id"]
    spectro_qc = json.loads(by_kind["spectrogram"]["qc"])
    assert spectro_qc["f_max_hz"] > 0 and spectro_qc["duration_s"] > 0
    rejected = next(r for r in rows if r["status"] == "rejected")
    fixed = next(r for r in rows if r["supersedes_id"] == rejected["id"])
    assert fixed["status"] == "verified"
    assert any("measured" in d for d in json.loads(rejected["defects"]))
    assert all(i["intuition_md"].startswith(SEED_PREFIX) for i in intuitions)
    assert all(v["source"] == "seed" for v in reviews)
    assert activity["source"] == "seed" and activity["region"] == "R2.critic"
    for r in rows:
        assert r["python_source"].startswith("OVERRIDES = ")


def test_seed_artifacts_are_real_media(cone, tmp_path):
    report = run(cone, tmp_path)
    store = tmp_path / "artifacts"
    audio = next(w for w in report["witnesses"] if w["kind"] == "audio")
    with wave.open(str(store / f"{audio['sha256']}.wav")) as fh:
        assert fh.getnchannels() == 1 and fh.getnframes() > fh.getframerate()  # more than a second of sound
    for w in report["witnesses"]:
        if w["ext"] == "png":
            assert (store / f"{w['sha256']}.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_seed_plots_are_drawn_for_the_light_page_and_the_flaw_is_measured(cone, tmp_path):
    report = run(cone, tmp_path)
    store = tmp_path / "artifacts"

    def pixels(w):
        return mpimg.imread(store / f"{w['sha256']}.png")[..., :3]

    plots = [w for w in report["witnesses"] if w["kind"] == "plot"]
    assert sorted(w["status"] for w in plots) == ["rejected", "verified", "verified", "verified"]
    for w in plots:  # the flawed render too: its defects are in the drawing, not in the paper
        img = pixels(w)
        border = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]])
        assert border.min() == 1.0, (w["decl"], w["status"])  # white card to every edge
        assert img.min() < 0.6  # and something is drawn on it

    # the spectrogram is full-bleed: silence is paper, each tone a dark line
    spectrogram = pixels(next(w for w in report["witnesses"] if w["kind"] == "spectrogram")).mean(axis=-1)
    rows = spectrogram.shape[0]
    assert spectrogram[: rows // 4].min() > 0.97  # above the highest tone nothing sounds
    assert np.median(spectrogram) > 0.95 and spectrogram.min() < 0.35

    # the clipped fraction the UI reports is the one a fresh evaluation of Z(t) gives
    p = SeedParams.quick()
    z = np.array([float(mpmath.siegelz(t)) for t in np.linspace(0.0, p.hardy_t_max, p.hardy_samples)])
    expected = float(np.mean(np.abs(z) > 1.0))
    assert 0.05 < expected < 0.95
    with connect_ro(tmp_path / "state" / "art.sqlite") as con:
        defects = json.loads(con.execute("SELECT defects FROM witnesses WHERE status = 'rejected'").fetchone()[0])
        python_source = con.execute("SELECT python_source FROM witnesses WHERE status = 'rejected'").fetchone()[0]
    measured = [d for d in defects if d.endswith("(measured)")]
    assert len(measured) == 1 and len([d for d in defects if d.endswith("(planted)")]) == 2
    percent = int(re.search(r"(\d+)% of the curve is cut off", measured[0]).group(1))
    assert abs(percent - 100 * expected) <= 0.51
    # the flawed render carries no painted warning: the verdict is the UI's job
    assert "#C81E1E" not in python_source and "rcParams" not in python_source


def test_seed_writes_one_stub_per_chapter_and_never_overwrites_real_text(cone, tmp_path):
    index, cone_path, _ = cone
    first = run(cone, tmp_path)
    db = tmp_path / "state" / "art.sqlite"
    con = sqlite3.connect(cone_path)
    slugs = [r[0] for r in con.execute("SELECT slug FROM pillars ORDER BY flow_ord")]
    con.close()
    assert first["chapters"] == {"total": len(slugs), "stubs": len(slugs), "kept": 0}
    with connect_ro(db) as con:
        rows = {r["pillar_slug"]: dict(r) for r in con.execute("SELECT * FROM chapters")}
    assert sorted(rows) == sorted(slugs)
    assert {r["source"] for r in rows.values()} == {"seed"} and all(r["call_id"] is None for r in rows.values())
    assert all(r["synthesis_md"].startswith(STUB_BADGE) for r in rows.values())
    assert "Chapter 1 of" in rows[slugs[0]]["synthesis_md"] and "`riemannHypothesis`" in rows[slugs[-1]]["synthesis_md"]
    # a chapter written by another source survives re-seeding untouched
    ArtifactStore(db).put_chapter(slugs[0], "Kept", "text from a model run", source="mock", call_id="call-1")
    second = run(cone, tmp_path)
    assert second["chapters"] == {"total": len(slugs), "stubs": len(slugs) - 1, "kept": 1}
    with connect_ro(db) as con:
        kept = dict(con.execute("SELECT * FROM chapters WHERE pillar_slug = ?", (slugs[0],)).fetchone())
        seeds = con.execute("SELECT COUNT(*) FROM chapters WHERE source = 'seed'").fetchone()[0]
    assert (kept["synthesis_md"], kept["source"], kept["call_id"]) == ("text from a model run", "mock", "call-1")
    assert seeds == len(slugs) - 1
