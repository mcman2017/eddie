"""Read-only API: health, budget snapshot, index stats/search, SPA serving, no LLM imports."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from eddie.ingest.build import build_index
from eddie.ingest.synthetic import write_sample_tree


@pytest.fixture
def client(tmp_path, monkeypatch):
    write_sample_tree(tmp_path / "tree")
    index = tmp_path / "index.sqlite"
    build_index(tmp_path / "tree", index, workers=1, log=lambda *_: None)
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><div id=root></div>")
    (web / "assets" / "app.js").write_text("console.log('eddie')")
    public = tmp_path / "public"
    public.mkdir()
    monkeypatch.setenv("EDDIE_INDEX_PATH", str(index))
    monkeypatch.setenv("EDDIE_PUBLIC_DIR", str(public))
    monkeypatch.setenv("EDDIE_WEB_DIST", str(web))
    from eddie.api.app import create_app

    return TestClient(create_app()), public


def test_health(client):
    c, _ = client
    body = c.get("/healthz").json()
    assert body["ok"] and body["index"] and body["web"]


def test_budget_snapshot_unavailable_then_available(client):
    c, public = client
    assert c.get("/api/budget").json() == {"available": False}
    (public / "status.json").write_text(json.dumps({"committed_usd": 0.42, "lifetime_cap_usd": 8.0}))
    body = c.get("/api/budget").json()
    assert body["available"] and body["committed_usd"] == 0.42


def test_index_stats_search_and_decl(client):
    c, _ = client
    stats = c.get("/api/index/stats").json()
    assert stats["decls"] >= 40 and "forecast_r1_all_signatures" in stats
    hits = c.get("/api/search", params={"q": "zeta"}).json()["results"]
    assert any(h["full_name"] == "riemannZeta_eq_zero_re" for h in hits)
    decl = c.get("/api/decl", params={"name": "riemannHypothesis"}).json()
    assert decl["matches"][0]["signature"] == "theorem riemannHypothesis : RiemannHypothesis"
    assert c.get("/api/search", params={"q": ""}).status_code == 422
    assert c.get("/api/search", params={"q": "x", "limit": 500}).status_code == 422


def test_spa_index_is_never_cached_and_assets_are_served(client):
    c, _ = client
    root = c.get("/")
    assert root.status_code == 200 and "id=root" in root.text
    assert root.headers["cache-control"] == "no-store"
    assert c.get("/some/deep/link").headers["cache-control"] == "no-store"
    assert c.get("/assets/app.js").text == "console.log('eddie')"
    assert c.get("/api/does-not-exist").status_code == 404


def test_api_module_never_loads_the_gateway_or_ledger():
    code = (
        "import sys, eddie.api.app\n"
        "bad = [m for m in sys.modules if m.startswith(('eddie.llm.gateway', 'eddie.llm.transport', "
        "'eddie.llm.adapters', 'eddie.llm.governor', 'eddie.llm.ledger'))]\n"
        "print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
