"""`data/state/eddie_artifacts.sqlite`: everything the 4-region pipeline produces.

This store is separate from the immutable index, the rebuildable cone and the sacred spend
ledger. Rows are keyed by `decl_full_name`, which survives index rebuilds, and carry the
`decl_id` as a convenience. Each row records its `source` (ember | dgx | mock | seed) and,
for model output, the ledger `call_id` / `response_id`, so it can be traced to telemetry.
An Ember row without both is refused: every `source='ember'` row must come from a real,
governed call.

It uses a rollback journal, so the read-only API can open it from a read-only mount.

Schema 2 adds the pipeline's own tables (the resumable step queue, every witness attempt,
and a display copy of each call's receipt) and a few nullable columns. The migration is
idempotent and runs inside one write transaction, so two processes opening an old file at
once cannot race.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "2"
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

-- schema 2: the pipeline's resumable step queue (one row per unit of work)
CREATE TABLE IF NOT EXISTS pipeline_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL,
    step_key TEXT NOT NULL UNIQUE,
    tag TEXT NOT NULL,
    kind TEXT NOT NULL,
    pillar_slug TEXT,
    decl_full_name TEXT,
    payload TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    source TEXT NOT NULL CHECK (source IN {SOURCES}),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pipeline_steps_phase ON pipeline_steps(phase, status);

-- schema 2: every R1.worker attempt, including the ones that never rendered an image
CREATE TABLE IF NOT EXISTS witness_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_full_name TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    origin TEXT NOT NULL,
    effort TEXT,
    outcome TEXT NOT NULL,
    title TEXT,
    caption TEXT,
    relation TEXT,
    python_source TEXT,
    stderr_tail TEXT,
    qc TEXT,
    critique TEXT,
    witness_id INTEGER REFERENCES witnesses(id),
    source TEXT NOT NULL CHECK (source IN {SOURCES}),
    call_id TEXT,
    response_id TEXT,
    critic_call_id TEXT,
    critic_response_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS witness_attempts_decl ON witness_attempts(decl_full_name, attempt);

-- schema 2: a display copy of each model call's receipt. The ledger stays the source of truth.
CREATE TABLE IF NOT EXISTS ember_calls (
    call_id TEXT PRIMARY KEY,
    step_key TEXT,
    tag TEXT,
    backend TEXT NOT NULL,
    region TEXT NOT NULL,
    effort TEXT NOT NULL,
    response_id TEXT,
    status TEXT,
    input_tokens INTEGER,
    cached_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cost_nano INTEGER NOT NULL DEFAULT 0,
    shadow_nano INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    prompt_version TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ember_calls_step ON ember_calls(step_key);

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

# Nullable columns that schema 2 adds to schema-1 tables.
V2_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("triage", "role", "TEXT"),
    ("triage", "area", "TEXT"),
    ("triage", "visualizability", "INTEGER"),
    ("triage", "visual_hint", "TEXT"),
    ("witnesses", "response_id", "TEXT"),
    ("witnesses", "attempt", "INTEGER"),
    ("witnesses", "origin", "TEXT"),
    ("witnesses", "caption", "TEXT"),
    ("witnesses", "relation", "TEXT"),
    ("reviews", "response_id", "TEXT"),
    ("chapters", "response_id", "TEXT"),
    ("chapters", "cost_nano", "INTEGER"),
    ("chapters", "headline", "TEXT"),
)


class ProvenanceError(ValueError):
    """An Ember row without the ledger call that produced it."""


def check_provenance(source: str, call_id: str | None, response_id: str | None) -> None:
    if source == "ember" and (not call_id or not response_id):
        raise ProvenanceError("an Ember row needs the ledger call_id and the response_id of the call that produced it")


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def migrate(con: sqlite3.Connection) -> None:
    """Bring an open write transaction's database to schema 2 (idempotent)."""
    for table, column, kind in V2_COLUMNS:
        if column not in _columns(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    con.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )


class ArtifactStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)  # executescript manages its own transaction
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()
        with self.tx() as con:
            migrate(con)

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

    @contextlib.contextmanager
    def _use(self, con: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """Join the caller's transaction, or open one of our own."""
        if con is not None:
            yield con
            return
        with self.tx() as own:
            yield own

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        try:
            con.row_factory = sqlite3.Row
            yield con
        finally:
            con.close()

    # --- work queue (the Night 1.5 budget plan) -------------------------------------------------

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

    # --- pipeline outputs -------------------------------------------------------------------------
    #
    # A seed write never replaces a row another source wrote: `seed-demo` can be re-run after
    # the pipeline without clobbering Ember output. Every other source replaces what is there.

    def put_triage(self, decl_full_name: str, gloss: str, *, source: str, decl_id: int | None = None,
                   call_id: str | None = None, response_id: str | None = None, role: str | None = None,
                   area: str | None = None, visualizability: int | None = None, visual_hint: str | None = None,
                   con: sqlite3.Connection | None = None) -> None:
        check_provenance(source, call_id, response_id)
        with self._use(con) as c:
            c.execute(
                "INSERT INTO triage (decl_full_name, decl_id, gloss, source, call_id, response_id, role, area, "
                "visualizability, visual_hint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(decl_full_name) DO UPDATE SET gloss = excluded.gloss, decl_id = excluded.decl_id, "
                "source = excluded.source, call_id = excluded.call_id, response_id = excluded.response_id, "
                "role = excluded.role, area = excluded.area, visualizability = excluded.visualizability, "
                "visual_hint = excluded.visual_hint "
                "WHERE excluded.source <> 'seed' OR triage.source = 'seed'",
                (decl_full_name, decl_id, gloss, source, call_id, response_id, role, area, visualizability,
                 visual_hint, time.time()),
            )

    def put_intuition(self, decl_full_name: str, intuition_md: str, *, source: str, decl_id: int | None = None,
                      formal_signature: str | None = None, citation: dict | list | None = None, verdict: str = "pending",
                      arbiter_notes: str | None = None, call_id: str | None = None, response_id: str | None = None,
                      con: sqlite3.Connection | None = None) -> None:
        check_provenance(source, call_id, response_id)
        now = time.time()
        with self._use(con) as c:
            c.execute(
                "INSERT INTO intuitions (decl_full_name, decl_id, intuition_md, formal_signature, citation, verdict, "
                "arbiter_notes, source, call_id, response_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(decl_full_name) DO UPDATE SET intuition_md = excluded.intuition_md, "
                "formal_signature = excluded.formal_signature, citation = excluded.citation, verdict = excluded.verdict, "
                "arbiter_notes = excluded.arbiter_notes, source = excluded.source, call_id = excluded.call_id, "
                "response_id = excluded.response_id, updated_at = excluded.updated_at "
                "WHERE excluded.source <> 'seed' OR intuitions.source = 'seed'",
                (decl_full_name, decl_id, intuition_md, formal_signature, json.dumps(citation) if citation else None,
                 verdict, arbiter_notes, source, call_id, response_id, now, now),
            )

    def add_witness(self, decl_full_name: str, *, kind: str, artifact_sha256: str, ext: str, source: str,
                    status: str = "pending", title: str | None = None, decl_id: int | None = None, bytes_: int | None = None,
                    python_source: str | None = None, sandbox_job_id: str | None = None, qc: dict | None = None,
                    defects: list[str] | None = None, critic_verdict: dict | None = None,
                    supersedes_id: int | None = None, pair_id: int | None = None, call_id: str | None = None,
                    response_id: str | None = None, attempt: int | None = None, origin: str | None = None,
                    caption: str | None = None, relation: str | None = None,
                    con: sqlite3.Connection | None = None) -> int:
        check_provenance(source, call_id, response_id)
        with self._use(con) as c:
            cur = c.execute(
                "INSERT INTO witnesses (decl_full_name, decl_id, kind, title, artifact_sha256, ext, bytes, python_source, "
                "sandbox_job_id, qc, status, defects, critic_verdict, supersedes_id, pair_id, source, call_id, response_id, "
                "attempt, origin, caption, relation, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decl_full_name, decl_id, kind, title, artifact_sha256, ext, bytes_, python_source, sandbox_job_id,
                 json.dumps(qc) if qc is not None else None, status, json.dumps(defects or []),
                 json.dumps(critic_verdict) if critic_verdict is not None else None, supersedes_id, pair_id, source,
                 call_id, response_id, attempt, origin, caption, relation, time.time()),
            )
            return int(cur.lastrowid)

    def update_witness(self, witness_id: int, *, status: str, defects: list[str] | None = None,
                       critic_verdict: dict | None = None, con: sqlite3.Connection | None = None) -> None:
        with self._use(con) as c:
            c.execute(
                "UPDATE witnesses SET status = ?, defects = ?, critic_verdict = ? WHERE id = ?",
                (status, json.dumps(defects or []), json.dumps(critic_verdict) if critic_verdict is not None else None,
                 witness_id),
            )

    def add_review(self, *, region: str, verdict: str, source: str, witness_id: int | None = None,
                   decl_full_name: str | None = None, reasons: list[str] | None = None, call_id: str | None = None,
                   response_id: str | None = None, con: sqlite3.Connection | None = None) -> None:
        check_provenance(source, call_id, response_id)
        with self._use(con) as c:
            c.execute(
                "INSERT INTO reviews (witness_id, decl_full_name, region, verdict, reasons, source, call_id, response_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (witness_id, decl_full_name, region, verdict, json.dumps(reasons or []), source, call_id, response_id,
                 time.time()),
            )

    def put_chapter(self, pillar_slug: str, title: str, synthesis_md: str, *, source: str, call_id: str | None = None,
                    response_id: str | None = None, cost_nano: int | None = None, headline: str | None = None,
                    con: sqlite3.Connection | None = None) -> None:
        check_provenance(source, call_id, response_id)
        with self._use(con) as c:
            c.execute(
                "INSERT INTO chapters (pillar_slug, title, synthesis_md, source, call_id, response_id, cost_nano, headline, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(pillar_slug) DO UPDATE SET title = excluded.title, synthesis_md = excluded.synthesis_md, "
                "source = excluded.source, call_id = excluded.call_id, response_id = excluded.response_id, "
                "cost_nano = excluded.cost_nano, headline = excluded.headline "
                "WHERE excluded.source <> 'seed' OR chapters.source = 'seed'",
                (pillar_slug, title, synthesis_md, source, call_id, response_id, cost_nano, headline, time.time()),
            )

    def put_chapter_stub(self, pillar_slug: str, title: str, synthesis_md: str) -> bool:
        """Store a seed placeholder unless the chapter already has text. A stub never replaces
        what a model (or another source) wrote; returns whether it was stored."""
        with self.tx() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO chapters (pillar_slug, title, synthesis_md, source, call_id, created_at) "
                "VALUES (?, ?, ?, 'seed', NULL, ?)",
                (pillar_slug, title, synthesis_md, time.time()),
            )
            return cur.rowcount == 1

    def set_activity(self, region: str | None, *, decl_full_name: str | None = None, message: str | None = None,
                     source: str = "seed", con: sqlite3.Connection | None = None) -> None:
        with self._use(con) as c:
            c.execute(
                "INSERT INTO activity (id, region, decl_full_name, message, source, updated_at) VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET region = excluded.region, decl_full_name = excluded.decl_full_name, "
                "message = excluded.message, source = excluded.source, updated_at = excluded.updated_at",
                (region, decl_full_name, message, source, time.time()),
            )

    # --- the pipeline's step queue, attempts and receipts ----------------------------------------

    def ensure_step(self, *, phase: str, step_key: str, tag: str, kind: str, source: str,
                    pillar_slug: str | None = None, decl_full_name: str | None = None,
                    payload: dict | None = None, con: sqlite3.Connection | None = None) -> sqlite3.Row:
        """Create a step once; later runs get the existing row (and its status) back."""
        now = time.time()
        with self._use(con) as c:
            c.execute(
                "INSERT OR IGNORE INTO pipeline_steps (phase, step_key, tag, kind, pillar_slug, decl_full_name, payload, "
                "source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (phase, step_key, tag, kind, pillar_slug, decl_full_name, json.dumps(payload or {}), source, now, now),
            )
            return c.execute("SELECT * FROM pipeline_steps WHERE step_key = ?", (step_key,)).fetchone()

    def step(self, step_key: str) -> sqlite3.Row | None:
        with self.read() as c:
            return c.execute("SELECT * FROM pipeline_steps WHERE step_key = ?", (step_key,)).fetchone()

    def steps(self, phase: str | None = None) -> list[sqlite3.Row]:
        with self.read() as c:
            if phase is None:
                return c.execute("SELECT * FROM pipeline_steps ORDER BY id").fetchall()
            return c.execute("SELECT * FROM pipeline_steps WHERE phase = ? ORDER BY id", (phase,)).fetchall()

    def mark_step(self, step_key: str, status: str, *, result: dict | None = None, error: str | None = None,
                  bump_attempt: bool = False, con: sqlite3.Connection | None = None) -> None:
        with self._use(con) as c:
            c.execute(
                "UPDATE pipeline_steps SET status = ?, result = COALESCE(?, result), error = ?, "
                "attempts = attempts + ?, updated_at = ? WHERE step_key = ?",
                (status, json.dumps(result) if result is not None else None, error, 1 if bump_attempt else 0,
                 time.time(), step_key),
            )

    def add_attempt(self, *, decl_full_name: str, attempt: int, origin: str, outcome: str, source: str,
                    effort: str | None = None, title: str | None = None, caption: str | None = None,
                    relation: str | None = None, python_source: str | None = None, stderr_tail: str | None = None,
                    qc: dict | None = None, critique: dict | list | None = None, witness_id: int | None = None,
                    call_id: str | None = None, response_id: str | None = None, critic_call_id: str | None = None,
                    critic_response_id: str | None = None, con: sqlite3.Connection | None = None) -> int:
        check_provenance(source, call_id, response_id)
        with self._use(con) as c:
            cur = c.execute(
                "INSERT INTO witness_attempts (decl_full_name, attempt, origin, effort, outcome, title, caption, relation, "
                "python_source, stderr_tail, qc, critique, witness_id, source, call_id, response_id, critic_call_id, "
                "critic_response_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decl_full_name, attempt, origin, effort, outcome, title, caption, relation, python_source, stderr_tail,
                 json.dumps(qc) if qc is not None else None, json.dumps(critique) if critique is not None else None,
                 witness_id, source, call_id, response_id, critic_call_id, critic_response_id, time.time()),
            )
            return int(cur.lastrowid)

    def record_call(self, *, call_id: str, backend: str, region: str, effort: str, step_key: str | None, tag: str | None,
                    response_id: str | None, status: str | None, input_tokens: int, cached_tokens: int,
                    output_tokens: int, reasoning_tokens: int, cost_nano: int, shadow_nano: int,
                    latency_ms: int | None, prompt_version: str | None, con: sqlite3.Connection | None = None) -> None:
        with self._use(con) as c:
            c.execute(
                "INSERT OR REPLACE INTO ember_calls (call_id, step_key, tag, backend, region, effort, response_id, status, "
                "input_tokens, cached_tokens, output_tokens, reasoning_tokens, cost_nano, shadow_nano, latency_ms, "
                "prompt_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (call_id, step_key, tag, backend, region, effort, response_id, status, input_tokens, cached_tokens,
                 output_tokens, reasoning_tokens, cost_nano, shadow_nano, latency_ms, prompt_version, time.time()),
            )

    def meta(self, key: str) -> str | None:
        with self.read() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str, con: sqlite3.Connection | None = None) -> None:
        with self._use(con) as c:
            c.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def clear_source(self, source: str) -> None:
        """Remove every row from one source (e.g. re-seeding replaces only seed rows)."""
        with self.tx() as con:
            con.execute("DELETE FROM reviews WHERE source = ?", (source,))
            con.execute("UPDATE witness_attempts SET witness_id = NULL WHERE witness_id IN "
                        "(SELECT id FROM witnesses WHERE source = ?)", (source,))
            con.execute("UPDATE witnesses SET supersedes_id = NULL, pair_id = NULL WHERE source = ?", (source,))
            con.execute("DELETE FROM witnesses WHERE source = ?", (source,))
            con.execute("DELETE FROM triage WHERE source = ?", (source,))
            con.execute("DELETE FROM intuitions WHERE source = ?", (source,))
            con.execute("DELETE FROM chapters WHERE source = ?", (source,))
            con.execute("DELETE FROM activity WHERE source = ?", (source,))
            con.execute("DELETE FROM witness_attempts WHERE source = ?", (source,))
            con.execute("DELETE FROM pipeline_steps WHERE source = ?", (source,))


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con
