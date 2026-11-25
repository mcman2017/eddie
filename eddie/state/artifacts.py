"""`data/state/eddie_artifacts.sqlite`: everything the 4-region pipeline produces.

This store is separate from the immutable index, the rebuildable cone and the sacred spend
ledger. Rows are keyed by `decl_full_name`, which survives index rebuilds, and carry the
`decl_id` as a convenience. Each row records its `source` (ember | dgx | mock | seed) and,
for model output, the ledger `call_id` / `response_id`, so it can be traced to telemetry.
It uses a rollback journal, so the read-only API can open it from a read-only mount.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
SOURCES = ("ember", "dgx", "mock", "seed")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS work_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_hash TEXT NOT NULL,
    seq INTEGER NOT NULL,
    tier INTEGER NOT NULL CHECK (tier IN (2, 3, 4)),
    kind TEXT NOT NULL CHECK (kind IN ('triage_batch', 'milestone', 'consolidation')),
    pillar_slug TEXT,
    decl_full_name TEXT,
    decl_id INTEGER,
    payload TEXT NOT NULL,
    reserve_nano INTEGER NOT NULL CHECK (reserve_nano >= 0),
    expected_nano INTEGER NOT NULL CHECK (expected_nano >= 0),
    scheduled_day TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
    actual_nano INTEGER CHECK (actual_nano IS NULL OR actual_nano >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS work_items_status ON work_items(status, tier, seq);

CREATE TABLE IF NOT EXISTS triage (
    decl_full_name TEXT PRIMARY KEY,
    decl_id INTEGER,
    gloss TEXT NOT NULL,
    work_item_id INTEGER,
    source TEXT NOT NULL CHECK (source IN {SOURCES}),
    call_id TEXT,
    response_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS intuitions (
    decl_full_name TEXT PRIMARY KEY,
    decl_id INTEGER,
    intuition_md TEXT NOT NULL,
    formal_signature TEXT,
    citation TEXT,
    verdict TEXT NOT NULL DEFAULT 'pending' CHECK (verdict IN ('pending', 'verified', 'rejected')),
    arbiter_notes TEXT,
    source TEXT NOT NULL CHECK (source IN {SOURCES}),
    call_id TEXT,
    response_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS witnesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_full_name TEXT NOT NULL,
    decl_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('plot', 'audio', 'spectrogram', 'animation', 'data')),
    title TEXT,
    artifact_sha256 TEXT NOT NULL,
    ext TEXT NOT NULL,
    bytes INTEGER,
    python_source TEXT,
    sandbox_job_id TEXT,
    qc TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'rejected')),
    defects TEXT,
    critic_verdict TEXT,
    supersedes_id INTEGER REFERENCES witnesses(id),
    pair_id INTEGER REFERENCES witnesses(id),
    source TEXT NOT NULL CHECK (source IN {SOURCES}),
    call_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS witnesses_decl ON witnesses(decl_full_name);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    witness_id INTEGER REFERENCES witnesses(id),
    decl_full_name TEXT,
    region TEXT NOT NULL CHECK (region IN ('R2.critic', 'R3.arbiter')),
    verdict TEXT NOT NULL,
    reasons TEXT,
    source TEXT NOT NULL CHECK (source IN {SOURCES}),
    call_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    pillar_slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    synthesis_md TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN {SOURCES}),
    call_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    region TEXT,
    decl_full_name TEXT,
    message TEXT,
    source TEXT,
    updated_at REAL
);

CREATE VIEW IF NOT EXISTS v_decl_status AS
SELECT x.decl_full_name,
       CASE
           WHEN EXISTS (SELECT 1 FROM witnesses w WHERE w.decl_full_name = x.decl_full_name AND w.status = 'verified')
               THEN 'verified'
           WHEN EXISTS (SELECT 1 FROM triage t WHERE t.decl_full_name = x.decl_full_name)
               OR EXISTS (SELECT 1 FROM intuitions i WHERE i.decl_full_name = x.decl_full_name)
               THEN 'triaged'
           ELSE 'pending'
       END AS status
FROM (SELECT decl_full_name FROM witnesses
      UNION SELECT decl_full_name FROM triage
      UNION SELECT decl_full_name FROM intuitions) AS x;

CREATE VIEW IF NOT EXISTS v_pillar_progress AS
SELECT pillar_slug, tier,
       COUNT(*) AS items,
       SUM(status = 'done') AS done,
       SUM(reserve_nano) AS reserve_nano,
       SUM(COALESCE(actual_nano, 0)) AS actual_nano
FROM work_items GROUP BY pillar_slug, tier;
"""


class ArtifactStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)  # executescript manages its own transaction
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.executescript(SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            con.commit()
        finally:
            con.close()

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            con.close()

    # --- work queue ---------------------------------------------------------------------------

    def replace_pending_plan(self, plan_hash: str, items: list[dict[str, Any]]) -> int:
        """Swap in a new plan. Items already running/done/failed are never touched."""
        now = time.time()
        with self.tx() as con:
            con.execute("DELETE FROM work_items WHERE status = 'pending'")
            con.executemany(
                "INSERT INTO work_items (plan_hash, seq, tier, kind, pillar_slug, decl_full_name, decl_id, payload, "
                "reserve_nano, expected_nano, scheduled_day, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                [
                    (
                        plan_hash, it["seq"], it["tier"], it["kind"], it.get("pillar_slug"), it.get("decl_full_name"),
                        it.get("decl_id"), json.dumps(it["payload"]), it["reserve_nano"], it["expected_nano"],
                        it.get("scheduled_day"), now, now,
                    )
                    for it in items
                ],
            )
            con.execute(
                "INSERT INTO meta (key, value) VALUES ('plan_hash', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (plan_hash,),
            )
        return len(items)

    # --- pipeline outputs (used by tomorrow's regions and by seed-demo) -------------------------

    def put_triage(self, decl_full_name: str, gloss: str, *, source: str, decl_id: int | None = None,
                   call_id: str | None = None, response_id: str | None = None) -> None:
        with self.tx() as con:
            con.execute(
                "INSERT INTO triage (decl_full_name, decl_id, gloss, source, call_id, response_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(decl_full_name) DO UPDATE SET gloss = excluded.gloss, "
                "source = excluded.source, call_id = excluded.call_id, response_id = excluded.response_id",
                (decl_full_name, decl_id, gloss, source, call_id, response_id, time.time()),
            )

    def put_intuition(self, decl_full_name: str, intuition_md: str, *, source: str, decl_id: int | None = None,
                      formal_signature: str | None = None, citation: dict | None = None, verdict: str = "pending",
                      arbiter_notes: str | None = None, call_id: str | None = None) -> None:
        now = time.time()
        with self.tx() as con:
            con.execute(
                "INSERT INTO intuitions (decl_full_name, decl_id, intuition_md, formal_signature, citation, verdict, "
                "arbiter_notes, source, call_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(decl_full_name) DO UPDATE SET intuition_md = excluded.intuition_md, "
                "formal_signature = excluded.formal_signature, citation = excluded.citation, verdict = excluded.verdict, "
                "arbiter_notes = excluded.arbiter_notes, source = excluded.source, call_id = excluded.call_id, "
                "updated_at = excluded.updated_at",
                (decl_full_name, decl_id, intuition_md, formal_signature, json.dumps(citation) if citation else None,
                 verdict, arbiter_notes, source, call_id, now, now),
            )

    def add_witness(self, decl_full_name: str, *, kind: str, artifact_sha256: str, ext: str, source: str,
                    status: str = "pending", title: str | None = None, decl_id: int | None = None, bytes_: int | None = None,
                    python_source: str | None = None, sandbox_job_id: str | None = None, qc: dict | None = None,
                    defects: list[str] | None = None, critic_verdict: dict | None = None,
                    supersedes_id: int | None = None, pair_id: int | None = None, call_id: str | None = None) -> int:
        with self.tx() as con:
            cur = con.execute(
                "INSERT INTO witnesses (decl_full_name, decl_id, kind, title, artifact_sha256, ext, bytes, python_source, "
                "sandbox_job_id, qc, status, defects, critic_verdict, supersedes_id, pair_id, source, call_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decl_full_name, decl_id, kind, title, artifact_sha256, ext, bytes_, python_source, sandbox_job_id,
                 json.dumps(qc) if qc is not None else None, status, json.dumps(defects or []),
                 json.dumps(critic_verdict) if critic_verdict is not None else None, supersedes_id, pair_id, source,
                 call_id, time.time()),
            )
            return int(cur.lastrowid)

    def add_review(self, *, region: str, verdict: str, source: str, witness_id: int | None = None,
                   decl_full_name: str | None = None, reasons: list[str] | None = None, call_id: str | None = None) -> None:
        with self.tx() as con:
            con.execute(
                "INSERT INTO reviews (witness_id, decl_full_name, region, verdict, reasons, source, call_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (witness_id, decl_full_name, region, verdict, json.dumps(reasons or []), source, call_id, time.time()),
            )

    def put_chapter(self, pillar_slug: str, title: str, synthesis_md: str, *, source: str, call_id: str | None = None) -> None:
        with self.tx() as con:
            con.execute(
                "INSERT INTO chapters (pillar_slug, title, synthesis_md, source, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(pillar_slug) DO UPDATE SET title = excluded.title, synthesis_md = excluded.synthesis_md, "
                "source = excluded.source, call_id = excluded.call_id",
                (pillar_slug, title, synthesis_md, source, call_id, time.time()),
            )

    def set_activity(self, region: str | None, *, decl_full_name: str | None = None, message: str | None = None,
                     source: str = "seed") -> None:
        with self.tx() as con:
            con.execute(
                "INSERT INTO activity (id, region, decl_full_name, message, source, updated_at) VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET region = excluded.region, decl_full_name = excluded.decl_full_name, "
                "message = excluded.message, source = excluded.source, updated_at = excluded.updated_at",
                (region, decl_full_name, message, source, time.time()),
            )

    def clear_source(self, source: str) -> None:
        """Remove every row from one source (e.g. re-seeding replaces only seed rows)."""
        with self.tx() as con:
            con.execute("DELETE FROM reviews WHERE source = ?", (source,))
            con.execute("UPDATE witnesses SET supersedes_id = NULL, pair_id = NULL WHERE source = ?", (source,))
            con.execute("DELETE FROM witnesses WHERE source = ?", (source,))
            con.execute("DELETE FROM triage WHERE source = ?", (source,))
            con.execute("DELETE FROM intuitions WHERE source = ?", (source,))
            con.execute("DELETE FROM chapters WHERE source = ?", (source,))
            con.execute("DELETE FROM activity WHERE source = ?", (source,))


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con
