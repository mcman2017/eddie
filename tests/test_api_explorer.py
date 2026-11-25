"""Explorer API over the synthetic tree: overview, map zoom, inspector, artifacts, activity."""

from __future__ import annotations

import gzip
import sqlite3

import pytest
from fastapi.testclient import TestClient

from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone
from eddie.ingest.synthetic import write_sample_tree
from eddie.state.artifacts import ArtifactStore
from tests.helpers import make_png

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("explorer")
    write_sample_tree(root / "tree", scale=3)
    index = root / "index.sqlite"
    build_index(root / "tree", index, workers=1, log=lambda *_: None)
    con = sqlite3.connect(index)
    names = [r[0] for r in con.execute("SELECT full_name FROM decls WHERE full_name IS NOT NULL")]
    con.close()
    spine = root / "spine.tsv.gz"
    with gzip.open(spine, "wt") as fh:
        for n in names:
            if not n.startswith("Edge."):
                fh.write(f"{n}\tthm\n")
    cone = root / "cone.sqlite"
    build_cone(index, cone, spine_path=spine, workers=1, log=lambda *_: None)
    art_dir = root / "artifacts"
    art_dir.mkdir()
    png = make_png(64, 48)
    import hashlib

    sha = hashlib.sha256(png).hexdigest()
    (art_dir / f"{sha}.png").write_bytes(png)
    store = ArtifactStore(root / "state" / "eddie_artifacts.sqlite")
    bad = store.add_witness("riemannHypothesis", kind="plot", artifact_sha256=sha, ext="png", source="seed",
                            status="rejected", defects=["clipped"])
    store.add_witness("riemannHypothesis", kind="plot", artifact_sha256=sha, ext="png", source="seed",
                      status="verified", supersedes_id=bad)
    store.put_intuition("riemannHypothesis", "SEED: placeholder", source="seed")
    store.put_triage("Development.Spectral.Trace.critical_line_of_trace", "SEED gloss", source="seed")
    store.set_activity("R2.critic", decl_full_name="riemannHypothesis", message="reviewing")
    return {"index": index, "cone": cone, "art_dir": art_dir, "db": store.path, "sha": sha}


@pytest.fixture
def client(world, monkeypatch):
    monkeypatch.setenv("EDDIE_INDEX_PATH", str(world["index"]))
    monkeypatch.setenv("EDDIE_CONE_PATH", str(world["cone"]))
    monkeypatch.setenv("EDDIE_ARTIFACTS_DB", str(world["db"]))
    monkeypatch.setenv("EDDIE_ARTIFACT_DIR", str(world["art_dir"]))
    from eddie.api.app import create_app

    return TestClient(create_app())


def test_overview(client):
    o = client.get("/api/overview").json()
    assert o["archive"]["files"] == 14 and o["archive"]["sorry"] >= 1
    assert o["pillars"][0]["title"] == "Top-level reduction"
    assert [r["full_name"] for r in o["roots"]] == ["riemannHypothesis", "riemannZeta_eq_zero_re"]
    assert o["featured"][0]["full_name"] == "riemannHypothesis"


def test_map_zoom_from_root_to_declarations(client):
    root = client.get("/api/map/node/0").json()
    assert root["breadcrumb"] == [{"id": 0, "label": "Main", "kind": "root"}]
    pillars = root["children"]
    assert pillars[0]["label"] == "Top-level reduction"
    assert pillars[0]["verified"] == 1  # riemannHypothesis has a verified witness
    node = pillars[0]
    for _ in range(6):
        page = client.get(f"/api/map/node/{node['id']}").json()
        kids = page["children"]
        if kids and kids[0]["kind"] == "decl":
            break
        node = kids[0]
    statuses = {k["full_name"]: k["status"] for k in kids}
    assert statuses.get("riemannHypothesis") == "verified"


def test_inspector_payload(client, world):
    o = client.get("/api/overview").json()
    rh = o["roots"][0]["id"]
    d = client.get(f"/api/decl/{rh}", params={"body": True}).json()
    assert d["citation"]["path"] == "src/Main.lean" and d["citation"]["start_line"] >= 1
    assert d["metrics"]["is_root"] and d["metrics"]["depth"] == 0
    assert d["status"] == "verified"
    assert [w["status"] for w in d["witnesses"]] == ["rejected", "verified"]
    assert d["witnesses"][1]["supersedes_id"] == d["witnesses"][0]["id"]
    assert d["witnesses"][0]["defects"] == ["clipped"]
    assert d["body"].startswith("theorem riemannHypothesis")
    nb = client.get(f"/api/decl/{rh}/neighbors").json()
    assert any(u["full_name"].endswith("critical_line_of_trace") for u in nb["uses"])


def test_artifacts_are_served_and_validated(client, world):
    ok = client.get(f"/api/artifacts/{world['sha']}.png")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
    for bad in ("x.png", f"{world['sha']}.exe", f"{world['sha'][:-1]}Z.png", "%2e%2e%2fsecret.png"):
        assert client.get(f"/api/artifacts/{bad}").status_code == 404


def test_activity(client):
    a = client.get("/api/activity").json()
    assert a["region"] == "R2.critic" and a["decl_full_name"] == "riemannHypothesis"


def test_explorer_opens_databases_read_only(client, world):
    before = world["db"].stat().st_mtime_ns
    client.get("/api/overview")
    client.get("/api/map/node/0")
    assert world["db"].stat().st_mtime_ns == before
