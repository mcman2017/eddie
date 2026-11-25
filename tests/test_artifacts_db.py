import sqlite3

import pytest

from eddie.state.artifacts import ArtifactStore, connect_ro


def _item(seq, tier=3, kind="milestone"):
    return {"seq": seq, "tier": tier, "kind": kind, "decl_full_name": f"A.d{seq}", "decl_id": seq,
            "payload": {"x": seq}, "reserve_nano": 10, "expected_nano": 5, "scheduled_day": "2026-11-25"}


def test_schema_views_and_rollback_journal(tmp_path):
    store = ArtifactStore(tmp_path / "a.sqlite")
    with connect_ro(store.path) as con:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        views = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert mode == "delete"
    assert {"v_decl_status", "v_pillar_progress"} <= views


def test_replacing_a_plan_keeps_finished_work(tmp_path):
    store = ArtifactStore(tmp_path / "a.sqlite")
    store.replace_pending_plan("p1", [_item(1), _item(2)])
    with store.tx() as con:
        con.execute("UPDATE work_items SET status = 'done', actual_nano = 7 WHERE seq = 1")
    store.replace_pending_plan("p2", [_item(3), _item(4), _item(5)])
    with connect_ro(store.path) as con:
        rows = con.execute("SELECT plan_hash, seq, status FROM work_items ORDER BY seq").fetchall()
    assert [tuple(r) for r in rows] == [("p1", 1, "done"), ("p2", 3, "pending"), ("p2", 4, "pending"), ("p2", 5, "pending")]


def test_status_view_and_source_isolation(tmp_path):
    store = ArtifactStore(tmp_path / "a.sqlite")
    store.put_triage("A.t", "a gloss", source="mock")
    w = store.add_witness("A.v", kind="plot", artifact_sha256="ab" * 32, ext="png", source="seed", status="rejected")
    store.add_witness("A.v", kind="plot", artifact_sha256="cd" * 32, ext="png", source="seed", status="verified", supersedes_id=w)
    store.add_review(region="R2.critic", verdict="rejected", witness_id=w, source="seed", reasons=["clipped"])
    with connect_ro(store.path) as con:
        status = dict(con.execute("SELECT decl_full_name, status FROM v_decl_status").fetchall())
    assert status == {"A.t": "triaged", "A.v": "verified"}
    store.clear_source("seed")
    with connect_ro(store.path) as con:
        assert con.execute("SELECT COUNT(*) FROM witnesses").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM triage").fetchone()[0] == 1  # mock rows untouched


def test_constraints(tmp_path):
    store = ArtifactStore(tmp_path / "a.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        store.put_triage("A.t", "x", source="openai")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_witness("A.v", kind="video", artifact_sha256="ab" * 32, ext="mp4", source="seed")
