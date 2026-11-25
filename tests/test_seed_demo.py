"""seed-demo: sandbox witnesses attached to Main's theorems, all labeled seed, idempotent, $0."""

from __future__ import annotations

import gzip
import json
import sqlite3
import wave

import pytest

from eddie.demo.seed import SEED_PREFIX, SeedParams, Target, pick_targets, seed_demo
from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone
from eddie.ingest.synthetic import write_sample_tree
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
