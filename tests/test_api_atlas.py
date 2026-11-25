"""Atlas API over the synthetic tree: chapters in flow order, stub text, witnesses, and the landscape."""

from __future__ import annotations

import gzip
import hashlib
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from eddie import paths
from eddie.ingest.build import build_index
from eddie.ingest.cone import build_cone
from eddie.ingest.synthetic import write_sample_tree
from eddie.planner import zeta_landscape as zl
from eddie.planner.chapters import STUB_BADGE, chapter_stubs
from eddie.state.artifacts import ArtifactStore
from tests.helpers import make_png

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("atlas")
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
    art_dir = root / "artifacts"
    art_dir.mkdir()
    png = make_png(64, 48)
    sha = hashlib.sha256(png).hexdigest()
    (art_dir / f"{sha}.png").write_bytes(png)
    store = ArtifactStore(root / "state" / "eddie_artifacts.sqlite")
    bad = store.add_witness("riemannHypothesis", kind="plot", artifact_sha256=sha, ext="png", source="seed",
                            status="rejected", defects=["clipped"])
    good = store.add_witness("riemannHypothesis", kind="plot", artifact_sha256=sha, ext="png", source="seed",
                             status="verified", supersedes_id=bad)
    store.add_review(region="R2.critic", verdict="rejected", source="seed", witness_id=bad, decl_full_name="riemannHypothesis",
                     reasons=["SEED review"])
    stubs = chapter_stubs(cone, index)
    for s in stubs:
        assert store.put_chapter_stub(s["slug"], s["title"], s["markdown"])
    store.replace_pending_plan("plan1", [
        {"seq": 1, "tier": 3, "kind": "milestone", "pillar_slug": stubs[-1]["slug"], "decl_full_name": "riemannHypothesis",
         "payload": {}, "reserve_nano": 9, "expected_nano": 4, "scheduled_day": "2026-11-26"},
        {"seq": 2, "tier": 4, "kind": "consolidation", "pillar_slug": stubs[-1]["slug"], "payload": {}, "reserve_nano": 9,
         "expected_nano": 4, "scheduled_day": "2026-11-27"},
    ])
    landscape = root / "landscape"
    zl.build_landscape(index, cone, landscape, zl.Params.quick(), log=lambda *_: None)
    return {"index": index, "cone": cone, "db": store.path, "art_dir": art_dir, "landscape": landscape,
            "slugs": [s["slug"] for s in stubs], "good": good, "bad": bad, "root": root}


def make_client(world, monkeypatch, **override) -> TestClient:
    env = {"EDDIE_INDEX_PATH": world["index"], "EDDIE_CONE_PATH": world["cone"], "EDDIE_ARTIFACTS_DB": world["db"],
           "EDDIE_ARTIFACT_DIR": world["art_dir"], "EDDIE_LANDSCAPE_DIR": world["landscape"], **override}
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    from eddie.api.app import create_app

    return TestClient(create_app())


@pytest.fixture
def client(world, monkeypatch):
    return make_client(world, monkeypatch)


def test_chapters_follow_the_flow_and_end_at_main(client, world):
    body = client.get("/api/chapters").json()
    chapters = body["chapters"]
    assert [c["slug"] for c in chapters] == world["slugs"]
    assert [c["chapter"] for c in chapters] == list(range(1, len(chapters) + 1))
    assert chapters[-1]["tier"] == "main" and chapters[-1]["title"] == "Top-level reduction"
    assert all(c["tier"] != "main" for c in chapters[:-1])
    assert all(c["source"] == "seed" and c["badge"] == STUB_BADGE for c in chapters)
    assert chapters[0]["prev"] is None and chapters[-1]["next"] is None
    assert [c["next"] for c in chapters[:-1]] == world["slugs"][1:]
    assert chapters[-1]["verified_witnesses"] == 1 and chapters[-1]["node_id"] is not None
    assert body["stub_badge"] == STUB_BADGE and 0 < body["flow"]["forward_pct"] <= 100


def test_chapter_guide_has_structure_stub_text_and_the_rejected_verified_pair(client, world):
    d = client.get(f"/api/chapters/{world['slugs'][-1]}").json()
    assert d["badge"] == STUB_BADGE and d["source"] == "seed" and d["synthesis_md"].startswith(STUB_BADGE)
    assert d["tier_label"] == "Main" and d["chapter"] == d["chapters"] == len(world["slugs"])
    assert [w["status"] for w in d["witnesses"]] == ["rejected", "verified"]
    assert d["witnesses"][1]["supersedes_id"] == d["witnesses"][0]["id"] and d["witnesses"][0]["defects"] == ["clipped"]
    assert d["witnesses"][0]["url"].startswith("/api/artifacts/") and d["witnesses"][0]["decl_full_name"] == "riemannHypothesis"
    assert client.get(d["witnesses"][0]["url"]).status_code == 200
    assert d["reviews"] == [{"region": "R2.critic", "verdict": "rejected", "reasons": ["SEED review"], "source": "seed",
                            "witness_id": world["bad"]}]
    finals = {t["full_name"]: t for t in d["final_theorems"]}
    assert list(finals)[:2] == ["riemannHypothesis", "riemannZeta_eq_zero_re"] and finals["riemannHypothesis"]["status"] == "verified"
    assert finals["riemannHypothesis"]["signature"] == "theorem riemannHypothesis : RiemannHypothesis"
    assert all(b["full_name"] not in finals and b["dom_mass"] >= 2 for b in d["bottlenecks"])  # a gate gates something
    assert d["builds_on"] and all(n["net"] > 0 and n["slug"] in world["slugs"] for n in d["builds_on"]) and d["feeds"] == []
    assert d["planned"] == {"milestones": 1, "first_day": "2026-11-26", "consolidation_day": "2026-11-27"}
    first = client.get(f"/api/chapters/{world['slugs'][0]}").json()
    assert first["feeds"] and first["witnesses"] == [] and first["planned"] is None and first["prev"] is None
    assert first["final_theorems"] == []
    every = [client.get(f"/api/chapters/{slug}").json() for slug in world["slugs"]]
    assert any(c["bottlenecks"] and all(b["signature"] for b in c["bottlenecks"]) for c in every)
    assert client.get("/api/chapters/no-such-chapter").status_code == 404


def test_text_from_another_source_is_shown_without_the_stub_badge(world, monkeypatch, tmp_path):
    db = tmp_path / "state" / "art.sqlite"
    store = ArtifactStore(db)
    slug = world["slugs"][0]
    store.put_chapter(slug, "Written", "Real synthesis.", source="mock", call_id="c1")
    assert store.put_chapter_stub(slug, "Stub", STUB_BADGE) is False
    client = make_client(world, monkeypatch, EDDIE_ARTIFACTS_DB=db)
    d = client.get(f"/api/chapters/{slug}").json()
    assert (d["source"], d["badge"], d["synthesis_md"]) == ("mock", None, "Real synthesis.")
    listed = {c["slug"]: c for c in client.get("/api/chapters").json()["chapters"]}
    assert listed[slug]["badge"] is None and listed[world["slugs"][1]]["source"] is None  # no text yet: no badge, no source


def test_overview_carries_the_flow(client, world):
    o = client.get("/api/overview").json()
    assert o["pillars"][0]["title"] == "Top-level reduction"  # `ord` is unchanged: the core first
    assert o["pillars"][0]["tier"] == "main" and o["pillars"][0]["chapter"] == len(o["pillars"])
    assert sorted(p["chapter"] for p in o["pillars"]) == list(range(1, len(o["pillars"]) + 1))
    ids = {p["id"] for p in o["pillars"]}
    assert o["pillar_flows"] and all(f["from"] in ids and f["to"] in ids and f["net"] > 0 for f in o["pillar_flows"])
    chapter = {p["id"]: p["chapter"] for p in o["pillars"]}
    forward = sum(f["net"] for f in o["pillar_flows"] if chapter[f["from"]] < chapter[f["to"]])
    assert forward >= 0.5 * sum(f["net"] for f in o["pillar_flows"])
    assert 0 < o["flow"]["forward_pct"] <= 100


def test_landscape_is_served_as_built(client, world):
    res = client.get("/api/landscape")
    land = res.json()
    assert res.status_code == 200 and land["schema"] == "eddie-landscape/1" and land["stale"] is False
    assert [c["slug"] for c in land["chapters"]] == world["slugs"]
    blob = client.get("/api/landscape/fields", params={"v": land["build_id"]})
    assert blob.status_code == 200 and blob.headers["content-type"] == "application/octet-stream"
    expected = 0
    for b in land["fields"].values():
        n = 1
        for dim in b["shape"]:
            n *= dim
        expected += n
    assert len(blob.content) == 4 * expected == (world["landscape"] / "fields.f32").stat().st_size


def test_missing_landscape_is_a_503_with_a_hint_and_the_rest_still_works(world, monkeypatch, tmp_path):
    client = make_client(world, monkeypatch, EDDIE_LANDSCAPE_DIR=tmp_path / "nothing")
    for url in ("/api/landscape", "/api/landscape/fields"):
        res = client.get(url)
        assert res.status_code == 503 and "build-landscape" in res.json()["detail"]
    assert client.get("/api/chapters").status_code == 200 and client.get("/api/overview").status_code == 200


def test_a_rebuilt_cone_marks_the_landscape_stale(world, monkeypatch, tmp_path):
    cone = tmp_path / "cone.sqlite"
    shutil.copy(world["cone"], cone)
    con = sqlite3.connect(cone)
    con.execute("UPDATE cone_meta SET value = '2030-01-01T00:00:00+0000' WHERE key = 'built_at'")
    con.commit()
    con.close()
    assert make_client(world, monkeypatch, EDDIE_CONE_PATH=cone).get("/api/landscape").json()["stale"] is True


def test_an_old_cone_is_a_503_with_a_rebuild_hint(world, monkeypatch, tmp_path):
    cone = tmp_path / "cone.sqlite"
    shutil.copy(world["cone"], cone)
    con = sqlite3.connect(cone)
    con.execute("UPDATE cone_meta SET value = '1' WHERE key = 'cone_version'")
    con.commit()
    con.close()
    client = make_client(world, monkeypatch, EDDIE_CONE_PATH=cone)
    for url in ("/api/overview", "/api/chapters"):
        res = client.get(url)
        assert res.status_code == 503 and "cone build" in res.json()["detail"]


def test_atlas_opens_everything_read_only(client, world):
    files = [world["db"], world["cone"], world["index"], world["landscape"] / "landscape.json", world["landscape"] / "fields.f32"]
    before = [f.stat().st_mtime_ns for f in files]
    for url in ("/api/chapters", f"/api/chapters/{world['slugs'][-1]}", "/api/landscape", "/api/landscape/fields", "/api/overview"):
        assert client.get(url).status_code == 200
    assert [f.stat().st_mtime_ns for f in files] == before


def test_default_landscape_dir_rides_the_existing_index_mount(monkeypatch):
    assert paths.landscape_dir() == paths.data_dir() / "index" / "landscape"
    compose = (paths.repo_root() / "docker-compose.yml").read_text()
    assert "source: ./data/index" in compose and "target: /data/index" in compose  # the container already sees it
    monkeypatch.setenv("EDDIE_LANDSCAPE_DIR", "/somewhere/else")
    assert str(paths.landscape_dir()) == "/somewhere/else"
