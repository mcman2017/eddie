"""What the public deployment needs from the read-only API: no home paths, no proof bodies,
Ember receipts on model output, and data swapped under a `current` symlink seen at once."""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone
from eddie.ingest.synthetic import write_sample_tree
from eddie.planner.chapters import chapter_stubs
from eddie.state.artifacts import ArtifactStore

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("public")
    write_sample_tree(root / "tree", scale=2)
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
    return root, index, cone


def release(root, name, index, cone, *, chapter_text, witness=False):
    """A release directory laid out like the pod's /data/releases/<id>/."""
    rel = root / "releases" / name
    (rel / "index").mkdir(parents=True)
    shutil.copy(index, rel / "index" / "eddie_index.sqlite")
    shutil.copy(cone, rel / "index" / "eddie_cone.sqlite")
    store = ArtifactStore(rel / "state" / "eddie_artifacts.sqlite")
    for s in chapter_stubs(cone, index):
        store.put_chapter_stub(s["slug"], s["title"], s["markdown"])
    store.put_chapter("top-level-reduction", "Main", chapter_text, source="ember", call_id="call-1", response_id="resp-1",
                      cost_nano=4_100_000, headline="Where the proof ends")
    store.record_call(call_id="call-1", backend="ember", region="R4.consolidator", effort="high", step_key="r4:x", tag="d3:x",
                      response_id="resp-1", status="completed", input_tokens=9000, cached_tokens=0, output_tokens=6000,
                      reasoning_tokens=4000, cost_nano=4_100_000, shadow_nano=4_100_000, latency_ms=30_000,
                      prompt_version="d3.1")
    if witness:
        store.add_witness("riemannHypothesis", kind="plot", artifact_sha256="ab" * 32, ext="png", source="ember",
                          status="verified", call_id="call-2", response_id="resp-2")
    for f in (rel / "index" / "eddie_index.sqlite", rel / "index" / "eddie_cone.sqlite", rel / "state" / "eddie_artifacts.sqlite"):
        os.utime(f, (1_700_000_000, 1_700_000_000))  # identical mtimes in every release, on purpose
    return rel


@pytest.fixture
def pod(built, tmp_path, monkeypatch):
    root, index, cone = built
    data = tmp_path / "data"
    data.mkdir()
    a = release(data, "a", index, cone, chapter_text="first release")
    os.symlink(a, data / "current")
    monkeypatch.setenv("EDDIE_DATA_DIR", str(data / "current"))
    monkeypatch.setenv("EDDIE_INDEX_PATH", str(data / "current" / "index" / "eddie_index.sqlite"))
    monkeypatch.setenv("EDDIE_DOCS", "0")
    monkeypatch.setenv("EDDIE_WEB_DIST", str(tmp_path / "no-dist"))
    from eddie.api.app import create_app

    return TestClient(create_app()), data, (root, index, cone)


def test_stats_never_name_a_home_directory(pod):
    c, _, _ = pod
    meta = c.get("/api/index/stats").json()["meta"]
    assert "source_root" not in meta and "project_root" not in meta
    assert not any(isinstance(v, str) and v.startswith("/") for v in meta.values())


def test_proof_bodies_are_not_served_where_the_archive_is_absent(pod, monkeypatch):
    c, data, (root, _, _) = pod
    decl = c.get("/api/overview").json()["roots"][0]["id"]
    shutil.move(root / "tree", root / "tree-away")  # the pod has no extracted archive
    try:
        d = c.get(f"/api/decl/{decl}?body=1").json()
    finally:
        shutil.move(root / "tree-away", root / "tree")
    assert d["source_available"] is False and d["body"] is None and "not served" in d["body_note"]


def test_an_ember_chapter_carries_its_receipt(pod):
    c, _, _ = pod
    ch = c.get("/api/chapters/top-level-reduction").json()
    assert ch["source"] == "ember" and ch["badge"] is None and ch["headline"] == "Where the proof ends"
    assert ch["receipt"]["region"] == "R4.consolidator" and ch["receipt"]["response_id"] == "resp-1"
    assert ch["receipt"]["cost_usd"] == pytest.approx(0.0041)


def test_a_swapped_release_is_served_on_the_next_request(pod):
    c, data, (root, index, cone) = pod
    assert "first release" in c.get("/api/chapters/top-level-reduction").json()["synthesis_md"]
    assert "ember" not in c.get("/api/overview").json()["artifact_sources"]  # cached from here on
    b = release(data, "b", index, cone, chapter_text="second release", witness=True)
    tmp = data / "current.new"
    os.symlink(b, tmp)
    os.replace(tmp, data / "current")  # what `ln -sfn` + `mv -T` do in the pod
    assert "second release" in c.get("/api/chapters/top-level-reduction").json()["synthesis_md"]
    # same mtimes everywhere: only the resolved path tells the cache that the data changed
    assert c.get("/api/overview").json()["artifact_sources"].get("ember") == 1
