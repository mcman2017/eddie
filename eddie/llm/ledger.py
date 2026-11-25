"""The spend ledger: the single source of truth for Ember spend.

Design rules (each one closes a specific overspend path):

* One fresh SQLite connection per transaction (`isolation_level=None`, explicit
  `BEGIN IMMEDIATE`). Every gate check and the reservation insert run inside the same
  write-locked transaction, so processes, threads and tasks cannot race past a cap.
* WAL plus `synchronous=FULL`: a `dispatched` commit is durable before any byte is sent.
* Every state transition is a compare-and-swap with its rowcount checked.
* Committed spend never decreases: orphan charges are never refunded, and adjustments are
  non-negative. That lets the anchor keep a monotonic high-water mark.
* The ledger is bound to an anchor file outside its directory. A copied, restored,
  deleted or second ledger is refused for live calls.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from eddie.config import CONSECUTIVE_FAILURES_TO_TRIP, RESERVATION_TTL_S
from eddie.llm.errors import (
    BudgetExceededError,
    CircuitOpenError,
    KillSwitchError,
    LedgerIntegrityError,
    TransportGuardError,
)

SCHEMA_VERSION = 1
BUSY_TIMEOUT_S = 60.0
_GATE_DELAY_S = 0.0  # test hook: widens the gap between cap SUMs and the INSERT

INFLIGHT = ("reserved", "dispatched")
CHARGED = ("settled", "orphan_charged")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    pid_ns INTEGER,
    proc_start INTEGER,
    boot_id TEXT,
    host TEXT,
    argv TEXT,
    run_cap_nano INTEGER NOT NULL CHECK (run_cap_nano >= 0),
    started_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    billable INTEGER NOT NULL DEFAULT 1 CHECK (billable IN (0, 1)),
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    region TEXT NOT NULL,
    effort TEXT NOT NULL,
    tag TEXT,
    body_sha256 TEXT NOT NULL,
    est_input INTEGER NOT NULL CHECK (est_input >= 0),
    max_output INTEGER NOT NULL CHECK (max_output >= 16),
    bounded INTEGER NOT NULL CHECK (bounded IN (0, 1)),
    reserved_nano INTEGER NOT NULL CHECK (reserved_nano >= 0),
    w_nano INTEGER NOT NULL CHECK (w_nano >= 0),
    state TEXT NOT NULL
        CHECK (state IN ('reserved', 'dispatched', 'settled', 'released', 'orphan_charged')),
    created_at REAL NOT NULL,
    dispatched_at REAL,
    sent_at REAL,
    finished_at REAL,
    http_status INTEGER,
    response_id TEXT,
    status TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    cached_tokens INTEGER CHECK (cached_tokens IS NULL OR cached_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    actual_nano INTEGER CHECK (actual_nano IS NULL OR actual_nano >= 0),
    shadow_nano INTEGER CHECK (shadow_nano IS NULL OR shadow_nano >= 0),
    latency_ms INTEGER,
    anomalies TEXT,
    warnings TEXT,
    error TEXT,
    gate_total_nano INTEGER,
    CHECK (state NOT IN ('settled', 'orphan_charged') OR actual_nano IS NOT NULL),
    CHECK (state <> 'orphan_charged' OR actual_nano = reserved_nano),
    CHECK (state <> 'released' OR actual_nano IS NULL OR actual_nano = 0)
);
CREATE INDEX IF NOT EXISTS calls_by_state ON calls(state);
CREATE INDEX IF NOT EXISTS calls_by_run ON calls(run_id);
CREATE INDEX IF NOT EXISTS calls_by_finish ON calls(finished_at);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    nano INTEGER NOT NULL CHECK (nano >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('opening_balance', 'manual', 'recovery')),
    note TEXT
);

CREATE TABLE IF NOT EXISTS breaker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('closed', 'open')),
    reason TEXT,
    opened_at REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO breaker (id, state, consecutive_failures) VALUES (1, 'closed', 0);
"""


# --- process identity (for orphan detection) ----------------------------------------


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _proc_start_ticks(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    if fields[0] == "Z":  # zombie: gone for our purposes
        return None
    return int(fields[19])  # field 22 (starttime); fields after ')' start at field 3


def _pid_ns(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}/ns/pid").st_ino
    except OSError:
        return None


@dataclass(frozen=True)
class ProcIdentity:
    pid: int
    pid_ns: int | None
    proc_start: int | None
    boot_id: str
    host: str

    @classmethod
    def current(cls) -> ProcIdentity:
        pid = os.getpid()
        return cls(pid, _pid_ns(pid), _proc_start_ticks(pid), _boot_id(), socket.gethostname())


def process_alive(pid: int, proc_start: int | None) -> bool:
    start = _proc_start_ticks(pid)
    if start is None:
        return False
    return proc_start is None or start == proc_start


# --- anchor ---------------------------------------------------------------------------


def machine_id() -> str:
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        with contextlib.suppress(OSError):
            return Path(candidate).read_text().strip()
    return "unknown"


@dataclass
class Anchor:
    ledger_uuid: str
    ledger_realpath: str
    dev: int
    inode: int
    machine_id: str
    high_water_nano: int
    created_at: float

    @classmethod
    def read(cls, path: Path) -> Anchor | None:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise LedgerIntegrityError(f"anchor {path} is unreadable: {exc}") from exc
        return cls(**data)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(self), fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


@contextlib.contextmanager
def _anchor_lock(anchor_path: Path) -> Iterator[None]:
    anchor_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(anchor_path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- the ledger -------------------------------------------------------------------------


@dataclass(frozen=True)
class Caps:
    lifetime_nano: int
    daily_nano: int
    run_nano: int
    max_concurrency: int


class Ledger:
    """A spend ledger. `billable=False` is the shadow ledger for mock/dgx calls (no caps)."""

    def __init__(self, path: Path, *, billable: bool, create: bool = True) -> None:
        self.path = Path(path)
        self.billable = billable
        if not create and not self.path.exists():
            raise LedgerIntegrityError(
                f"no canonical ledger at {self.path}: run `python -m eddie.cli budget init` once"
            )
        created_dir = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created_dir and billable:
            os.chmod(self.path.parent, 0o700)
        new_file = not self.path.exists()
        with self._connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        if new_file:
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)

    # --- connections ----------------------------------------------------------------------

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
            yield con
        finally:
            con.close()

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as con:
            yield con

    # --- meta -------------------------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        with self.read() as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _persisted_lifetime_cap(self, con: sqlite3.Connection) -> int | None:
        row = con.execute("SELECT value FROM meta WHERE key = 'lifetime_cap_nano'").fetchone()
        return int(row["value"]) if row else None

    def set_lifetime_cap(self, nano: int) -> None:
        with self.tx() as con:
            con.execute(
                "INSERT INTO meta (key, value) VALUES ('lifetime_cap_nano', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(nano),),
            )

    # --- runs -------------------------------------------------------------------------------

    def register_run(self, run_id: str, run_cap_nano: int, *, ident: ProcIdentity | None = None) -> None:
        ident = ident or ProcIdentity.current()
        with self.tx() as con:
            con.execute(
                "INSERT INTO runs (run_id, pid, pid_ns, proc_start, boot_id, host, argv, run_cap_nano, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    ident.pid,
                    ident.pid_ns,
                    ident.proc_start,
                    ident.boot_id,
                    ident.host,
                    json.dumps(sys.argv[:6]),
                    run_cap_nano,
                    time.time(),
                ),
            )

    # --- aggregate queries ------------------------------------------------------------------

    @staticmethod
    def _committed(con: sqlite3.Connection) -> int:
        calls = con.execute(
            "SELECT COALESCE(SUM(actual_nano), 0) FROM calls WHERE billable = 1 AND state IN (?, ?)",
            CHARGED,
        ).fetchone()[0]
        adjustments = con.execute("SELECT COALESCE(SUM(nano), 0) FROM adjustments").fetchone()[0]
        return int(calls) + int(adjustments)

    @staticmethod
    def _inflight(con: sqlite3.Connection) -> tuple[int, int, int]:
        row = con.execute(
            "SELECT COALESCE(SUM(w_nano), 0), COALESCE(SUM(reserved_nano), 0), COUNT(*) "
            "FROM calls WHERE billable = 1 AND state IN (?, ?)",
            INFLIGHT,
        ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    @staticmethod
    def _daily_committed(con: sqlite3.Connection, now: float) -> int:
        since = now - 86_400
        calls = con.execute(
            "SELECT COALESCE(SUM(actual_nano), 0) FROM calls "
            "WHERE billable = 1 AND state IN (?, ?) AND finished_at >= ?",
            (*CHARGED, since),
        ).fetchone()[0]
        manual = con.execute(
            "SELECT COALESCE(SUM(nano), 0) FROM adjustments WHERE kind = 'manual' AND created_at >= ?",
            (since,),
        ).fetchone()[0]
        return int(calls) + int(manual)

    @staticmethod
    def _run_totals(con: sqlite3.Connection, run_id: str) -> int:
        row = con.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN state IN ('settled', 'orphan_charged') THEN actual_nano END), 0), "
            "COALESCE(SUM(CASE WHEN state IN ('reserved', 'dispatched') THEN reserved_nano END), 0) "
            "FROM calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row[0]) + int(row[1])

    def committed_nano(self) -> int:
        with self.read() as con:
            return self._committed(con)

    # --- orphan sweep -----------------------------------------------------------------------

    def _sweep(self, con: sqlite3.Connection, now: float) -> None:
        me = ProcIdentity.current()
        rows = con.execute(
            "SELECT c.id, c.state, c.created_at, r.pid, r.pid_ns, r.proc_start, r.boot_id "
            "FROM calls c JOIN runs r ON r.run_id = c.run_id WHERE c.state IN (?, ?)",
            INFLIGHT,
        ).fetchall()
        for row in rows:
            expired = now - row["created_at"] > RESERVATION_TTL_S
            same_host = row["boot_id"] == me.boot_id and row["pid_ns"] == me.pid_ns
            dead = same_host and not process_alive(row["pid"], row["proc_start"])
            if not (expired or dead):
                continue
            why = "swept: reservation TTL exceeded" if expired else "swept: owning process is gone"
            if row["state"] == "reserved":
                con.execute(
                    "UPDATE calls SET state = 'released', finished_at = ?, error = ? "
                    "WHERE id = ? AND state = 'reserved'",
                    (now, why, row["id"]),
                )
            else:
                con.execute(
                    "UPDATE calls SET state = 'orphan_charged', actual_nano = reserved_nano, "
                    "finished_at = ?, error = ? WHERE id = ? AND state = 'dispatched'",
                    (now, why, row["id"]),
                )

    def sweep(self, now: float | None = None) -> None:
        with self.tx() as con:
            self._sweep(con, now or time.time())

    # --- reservation ------------------------------------------------------------------------

    def reserve(
        self,
        *,
        run_id: str,
        backend: str,
        model: str,
        region: str,
        effort: str,
        tag: str | None,
        body_sha256: str,
        est_input: int,
        max_output: int,
        bounded: bool,
        reserved_nano: int,
        w_nano: int,
        caps: Caps | None,
        kill_switch: Path | None = None,
        now: float | None = None,
    ) -> str:
        now = now or time.time()
        call_id = uuid.uuid4().hex
        if self.billable:
            # Committed on its own, so a refused reservation cannot roll back the sweep and
            # leave stale rows blocking every future reservation.
            self.sweep(now)
        with self.tx() as con:
            gate_total = None
            if self.billable:
                if caps is None:
                    raise ValueError("billable reservations need caps")
                if kill_switch is not None and kill_switch.exists():
                    raise KillSwitchError(f"kill switch {kill_switch} is present")
                breaker = con.execute("SELECT state, reason FROM breaker WHERE id = 1").fetchone()
                if breaker["state"] == "open":
                    raise CircuitOpenError(f"breaker open: {breaker['reason']}")

                persisted = self._persisted_lifetime_cap(con)
                lifetime = caps.lifetime_nano if persisted is None else min(caps.lifetime_nano, persisted)
                committed = self._committed(con)
                inflight_w, inflight_reserved, inflight_count = self._inflight(con)
                if committed + inflight_w + w_nano > lifetime:
                    raise BudgetExceededError(
                        "lifetime",
                        f"committed {committed} + in-flight worst case {inflight_w} + this call's "
                        f"worst case {w_nano} nUSD would exceed {lifetime} nUSD",
                    )
                daily = self._daily_committed(con, now)
                if daily + inflight_reserved + reserved_nano > caps.daily_nano:
                    raise BudgetExceededError(
                        "daily", f"rolling 24 h spend {daily} + in-flight {inflight_reserved} + {reserved_nano} nUSD > {caps.daily_nano}"
                    )
                run_total = self._run_totals(con, run_id)
                if run_total + reserved_nano > caps.run_nano:
                    raise BudgetExceededError(
                        "run", f"run spend+reserved {run_total} + {reserved_nano} nUSD > {caps.run_nano}"
                    )
                if inflight_count >= caps.max_concurrency:
                    raise BudgetExceededError("concurrency", f"{inflight_count} calls already in flight")
                gate_total = committed + inflight_w + w_nano
                if _GATE_DELAY_S:
                    time.sleep(_GATE_DELAY_S)
            con.execute(
                "INSERT INTO calls (id, run_id, billable, backend, model, region, effort, tag, "
                "body_sha256, est_input, max_output, bounded, reserved_nano, w_nano, state, "
                "created_at, gate_total_nano) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)",
                (
                    call_id,
                    run_id,
                    1 if self.billable else 0,
                    backend,
                    model,
                    region,
                    effort,
                    tag,
                    body_sha256,
                    est_input,
                    max_output,
                    1 if bounded else 0,
                    reserved_nano,
                    w_nano,
                    now,
                    gate_total,
                ),
            )
        return call_id

    def mark_dispatched(self, call_id: str, *, now: float | None = None) -> None:
        with self.tx() as con:
            cur = con.execute(
                "UPDATE calls SET state = 'dispatched', dispatched_at = ? WHERE id = ? AND state = 'reserved'",
                (now or time.time(), call_id),
            )
            if cur.rowcount != 1:
                raise LedgerIntegrityError(f"reservation {call_id} is no longer reserved (swept or reused)")

    def claim_send(self, call_id: str, body_sha256: str, *, now: float | None = None) -> sqlite3.Row:
        """Single-use authorization, consumed by the transport right before the socket opens."""
        with self.tx() as con:
            row = con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
            if row is None:
                raise TransportGuardError("unknown reservation")
            if row["state"] != "dispatched":
                raise TransportGuardError(f"reservation is {row['state']}, not dispatched")
            if row["sent_at"] is not None:
                raise TransportGuardError("reservation was already used for a send")
            if row["body_sha256"] != body_sha256:
                raise TransportGuardError("body changed after reservation")
            cur = con.execute(
                "UPDATE calls SET sent_at = ? WHERE id = ? AND state = 'dispatched' AND sent_at IS NULL",
                (now or time.time(), call_id),
            )
            if cur.rowcount != 1:
                raise TransportGuardError("reservation was claimed concurrently")
            return row

    def get_call(self, call_id: str) -> sqlite3.Row | None:
        with self.read() as con:
            return con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()

    # --- settlement -------------------------------------------------------------------------

    def settle(
        self,
        call_id: str,
        *,
        actual_nano: int,
        shadow_nano: int,
        input_tokens: int | None,
        cached_tokens: int | None,
        output_tokens: int | None,
        reasoning_tokens: int | None,
        response_id: str | None,
        status: str | None,
        http_status: int | None,
        latency_ms: int | None,
        anomalies: list[str] | None = None,
        warnings: list[str] | None = None,
        trip_reason: str | None = None,
        now: float | None = None,
    ) -> None:
        now = now or time.time()
        with self.tx() as con:
            row = con.execute("SELECT state, reserved_nano FROM calls WHERE id = ?", (call_id,)).fetchone()
            if row is None or row["state"] not in ("dispatched", "orphan_charged"):
                raise LedgerIntegrityError(f"cannot settle call {call_id} in state {row['state'] if row else None}")
            charge = actual_nano
            if row["state"] == "orphan_charged":  # never refund an orphan charge
                charge = max(actual_nano, row["reserved_nano"])
            cur = con.execute(
                "UPDATE calls SET state = 'settled', actual_nano = ?, shadow_nano = ?, input_tokens = ?, "
                "cached_tokens = ?, output_tokens = ?, reasoning_tokens = ?, response_id = ?, status = ?, "
                "http_status = ?, latency_ms = ?, anomalies = ?, warnings = ?, finished_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    charge,
                    shadow_nano,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    reasoning_tokens,
                    response_id,
                    status,
                    http_status,
                    latency_ms,
                    json.dumps(anomalies or []),
                    json.dumps(warnings or []),
                    now,
                    call_id,
                    row["state"],
                ),
            )
            if cur.rowcount != 1:
                raise LedgerIntegrityError(f"settlement of {call_id} lost a race")
            if self.billable:
                if trip_reason:
                    self._trip(con, trip_reason, now)
                else:
                    con.execute("UPDATE breaker SET consecutive_failures = 0 WHERE id = 1")

    def release(self, call_id: str, *, http_status: int | None, error: str, trip_reason: str | None = None) -> None:
        now = time.time()
        with self.tx() as con:
            cur = con.execute(
                "UPDATE calls SET state = 'released', http_status = ?, error = ?, finished_at = ? "
                "WHERE id = ? AND state IN ('reserved', 'dispatched')",
                (http_status, error[:500], now, call_id),
            )
            if cur.rowcount != 1:
                raise LedgerIntegrityError(f"cannot release call {call_id}")
            if self.billable and trip_reason:
                self._trip(con, trip_reason, now)

    def orphan_charge(self, call_id: str, *, http_status: int | None, error: str) -> None:
        """The request may have been billed but no usage came back: charge the full reservation."""
        now = time.time()
        with self.tx() as con:
            row = con.execute("SELECT state FROM calls WHERE id = ?", (call_id,)).fetchone()
            if row is not None and row["state"] == "reserved":  # never sent: nothing to charge
                con.execute(
                    "UPDATE calls SET state = 'released', http_status = ?, error = ?, finished_at = ? "
                    "WHERE id = ? AND state = 'reserved'",
                    (http_status, error[:500], now, call_id),
                )
                return
            cur = con.execute(
                "UPDATE calls SET state = 'orphan_charged', actual_nano = reserved_nano, http_status = ?, "
                "error = ?, finished_at = ? WHERE id = ? AND state = 'dispatched'",
                (http_status, error[:500], now, call_id),
            )
            if cur.rowcount != 1:
                raise LedgerIntegrityError(f"cannot orphan-charge call {call_id}")
            if self.billable:
                con.execute("UPDATE breaker SET consecutive_failures = consecutive_failures + 1 WHERE id = 1")
                fails = con.execute("SELECT consecutive_failures FROM breaker WHERE id = 1").fetchone()[0]
                if fails >= CONSECUTIVE_FAILURES_TO_TRIP:
                    self._trip(con, f"{fails} consecutive failed calls (last: {error[:120]})", now)

    # --- breaker & adjustments ----------------------------------------------------------------

    @staticmethod
    def _trip(con: sqlite3.Connection, reason: str, now: float) -> None:
        con.execute(
            "UPDATE breaker SET state = 'open', reason = ?, opened_at = ? WHERE id = 1",
            (reason[:1000], now),
        )

    def trip(self, reason: str) -> None:
        with self.tx() as con:
            self._trip(con, reason, time.time())

    def reset_breaker(self) -> None:
        with self.tx() as con:
            con.execute(
                "UPDATE breaker SET state = 'closed', reason = NULL, opened_at = NULL, "
                "consecutive_failures = 0 WHERE id = 1"
            )

    def breaker(self) -> dict[str, Any]:
        with self.read() as con:
            return dict(con.execute("SELECT * FROM breaker WHERE id = 1").fetchone())

    def add_adjustment(self, nano: int, *, kind: str, note: str | None = None) -> None:
        if nano < 0:
            raise ValueError("adjustments must be non-negative (committed spend never decreases)")
        with self.tx() as con:
            con.execute(
                "INSERT INTO adjustments (created_at, nano, kind, note) VALUES (?, ?, ?, ?)",
                (time.time(), nano, kind, note),
            )

    # --- reporting ------------------------------------------------------------------------------

    def totals(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        with self.read() as con:
            committed = self._committed(con)
            inflight_w, inflight_reserved, inflight_count = self._inflight(con)
            daily = self._daily_committed(con, now)
            persisted = self._persisted_lifetime_cap(con)
            states = {r[0]: r[1] for r in con.execute("SELECT state, COUNT(*) FROM calls GROUP BY state")}
            shadow = con.execute("SELECT COALESCE(SUM(shadow_nano), 0) FROM calls").fetchone()[0]
            adjustments = con.execute("SELECT COALESCE(SUM(nano), 0) FROM adjustments").fetchone()[0]
            anomalous = con.execute(
                "SELECT COUNT(*) FROM calls WHERE anomalies IS NOT NULL AND anomalies <> '[]'"
            ).fetchone()[0]
        return {
            "committed_nano": committed,
            "adjustments_nano": int(adjustments),
            "inflight_reserved_nano": inflight_reserved,
            "inflight_worst_case_nano": inflight_w,
            "inflight_count": inflight_count,
            "daily_committed_nano": daily,
            "persisted_lifetime_cap_nano": persisted,
            "shadow_nano": int(shadow),
            "calls_by_state": states,
            "anomalous_calls": int(anomalous),
        }

    def charged_calls(self, *, include_nonbillable: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM calls WHERE state IN ('settled', 'orphan_charged')"
        if not include_nonbillable:
            query += " AND billable = 1"
        with self.read() as con:
            return con.execute(query + " ORDER BY finished_at, created_at").fetchall()

    def adjustments(self) -> list[sqlite3.Row]:
        with self.read() as con:
            return con.execute("SELECT * FROM adjustments ORDER BY created_at, id").fetchall()

    def recent_calls(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.read() as con:
            return con.execute("SELECT * FROM calls ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    # --- anchoring -------------------------------------------------------------------------------

    def ledger_uuid(self) -> str | None:
        return self.meta("ledger_uuid")

    def verify_anchor(self, anchor_path: Path) -> Anchor:
        anchor = Anchor.read(anchor_path)
        if anchor is None:
            raise LedgerIntegrityError(
                f"no ledger anchor at {anchor_path}: run `python -m eddie.cli budget init` once"
            )
        st = os.stat(self.path)
        problems = []
        if self.ledger_uuid() != anchor.ledger_uuid:
            problems.append("ledger uuid does not match its anchor (a second ledger?)")
        if str(self.path.resolve()) != anchor.ledger_realpath:
            problems.append("ledger path does not match its anchor (moved or a second ledger?)")
        if (st.st_dev, st.st_ino) != (anchor.dev, anchor.inode):
            problems.append("ledger file identity changed (copied or restored?)")
        if machine_id() != anchor.machine_id:
            problems.append("ledger anchored on a different machine")
        committed = self.committed_nano()
        if committed < anchor.high_water_nano:
            problems.append(
                f"ledger shows {committed} nUSD committed but its anchor recorded {anchor.high_water_nano} (rolled back?)"
            )
        if problems:
            raise LedgerIntegrityError("; ".join(problems))
        return anchor

    def raise_high_water(self, anchor_path: Path) -> None:
        with _anchor_lock(anchor_path):
            anchor = Anchor.read(anchor_path)
            if anchor is None or anchor.ledger_uuid != self.ledger_uuid():
                return
            committed = self.committed_nano()
            if committed > anchor.high_water_nano:
                anchor.high_water_nano = committed
                anchor.write(anchor_path)


def initialize_canonical_ledger(
    ledger_path: Path,
    anchor_path: Path,
    *,
    lifetime_cap_nano: int,
    opening_balance_nano: int,
    note: str | None = None,
    other_anchor_paths: tuple[Path, ...] = (),
    recover: bool = False,
) -> Ledger:
    """Create THE canonical ledger and its anchor. Refuses to create a second one.

    Recovery (`recover=True`) is the only way past an existing anchor: the old ledger
    must be gone, and the dashboard figure must be at least the recorded high-water mark.
    """
    existing = Anchor.read(anchor_path)
    for other in other_anchor_paths:
        if other != anchor_path and other.exists():
            raise LedgerIntegrityError(f"an anchor already exists at {other}; refusing to create a second ledger")
    if ledger_path.exists():
        raise LedgerIntegrityError(f"{ledger_path} already exists; refusing to re-initialize it")
    if existing is not None and not recover:
        raise LedgerIntegrityError(
            f"anchor {anchor_path} already exists (high-water {existing.high_water_nano} nUSD). "
            "If the ledger was lost, use `budget init --recover --dashboard-usd X`."
        )
    if recover:
        if existing is None:
            raise LedgerIntegrityError("nothing to recover: no anchor exists")
        if opening_balance_nano < existing.high_water_nano:
            raise LedgerIntegrityError(
                f"recovery balance {opening_balance_nano} nUSD is below the anchor high-water "
                f"{existing.high_water_nano} nUSD; enter the real dashboard figure"
            )

    ledger = Ledger(ledger_path, billable=True)
    ledger_id = uuid.uuid4().hex
    with ledger.tx() as con:
        for key, value in (
            ("ledger_uuid", ledger_id),
            ("canonical", "1"),
            ("created_at", str(time.time())),
            ("lifetime_cap_nano", str(lifetime_cap_nano)),
        ):
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        if opening_balance_nano > 0:
            con.execute(
                "INSERT INTO adjustments (created_at, nano, kind, note) VALUES (?, ?, ?, ?)",
                (time.time(), opening_balance_nano, "recovery" if recover else "opening_balance", note),
            )
    st = os.stat(ledger_path)
    Anchor(
        ledger_uuid=ledger_id,
        ledger_realpath=str(ledger_path.resolve()),
        dev=st.st_dev,
        inode=st.st_ino,
        machine_id=machine_id(),
        high_water_nano=opening_balance_nano,
        created_at=time.time(),
    ).write(anchor_path)
    return ledger
