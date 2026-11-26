"""Bounded declaration cards: the only archive text any model call carries.

Every field is capped in UTF-8 bytes (the governor's estimator counts bytes, so a byte cap is a
token cap). The caps match the carry caps recorded in `work_plan.json`, which is what keeps
every downstream bound exact.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SIG_CAP = 1_200
CTX_CAP = 1_500
BODY_CAP = 3_000
DOC_CAP = 600
NEIGHBOURS = 8
CARRY_INTUITION = 2_000
CARRY_CODE = 6_000
CARRY_CRITIQUE = 1_500
TRUNCATED = " …[truncated]"


def clip(text: str | None, cap: int) -> str:
    """At most `cap` UTF-8 bytes, cut on a character boundary, marked when cut."""
    if not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    keep = max(0, cap - len(TRUNCATED.encode("utf-8")))
    return raw[:keep].decode("utf-8", "ignore") + TRUNCATED


@dataclass(frozen=True)
class Card:
    id: int
    full_name: str
    kind: str
    module: str
    path: str
    start_line: int
    end_line: int
    signature: str
    context: str
    docstring: str

    @property
    def cite(self) -> str:
        lines = f"{self.start_line}" if self.start_line == self.end_line else f"{self.start_line}-{self.end_line}"
        return f"{self.module} (src/{self.path}:{lines})"


_CARD_SQL = (
    "SELECT d.id, d.full_name, d.kind, d.signature, d.context, d.docstring, d.start_line, d.end_line, "
    "mo.name AS module, mo.path AS path FROM idx.decls d JOIN idx.modules mo ON mo.id = d.module_id "
)


def _card(r: sqlite3.Row) -> Card:
    return Card(int(r["id"]), r["full_name"] or f"(anonymous {r['kind']})", r["kind"], r["module"], r["path"],
                int(r["start_line"]), int(r["end_line"]), clip(r["signature"], SIG_CAP), clip(r["context"], CTX_CAP),
                clip(r["docstring"], DOC_CAP))


def card(con: sqlite3.Connection, decl_id: int) -> Card:
    """`con` is the cone database with the index attached as `idx`."""
    row = con.execute(_CARD_SQL + "WHERE d.id = ?", (decl_id,)).fetchone()
    if row is None:
        raise KeyError(f"no declaration {decl_id}")
    return _card(row)


def cards(con: sqlite3.Connection, ids: list[int]) -> list[Card]:
    return [card(con, i) for i in ids]


def neighbours(con: sqlite3.Connection, decl_id: int, k: int = NEIGHBOURS) -> list[Card]:
    """What the declaration uses, most important first (the planner's own neighbour query)."""
    rows = con.execute(
        _CARD_SQL + "JOIN edges e ON e.dst = d.id JOIN decl_metrics m ON m.decl_id = d.id "
        "WHERE e.src = ? AND m.live = 1 ORDER BY m.score DESC, e.dst LIMIT ?",
        (decl_id, k),
    ).fetchall()
    return [_card(r) for r in rows]


def body_excerpt(con: sqlite3.Connection, decl_id: int, cap: int = BODY_CAP) -> str | None:
    """The first `cap` bytes of the proof, read from the extracted archive (host only)."""
    meta = dict(con.execute("SELECT key, value FROM idx.index_meta").fetchall())
    row = con.execute("SELECT d.start_byte, d.end_byte, mo.path FROM idx.decls d JOIN idx.modules mo ON mo.id = d.module_id "
                      "WHERE d.id = ?", (decl_id,)).fetchone()
    if row is None or "source_root" not in meta:
        return None
    src = Path(meta["source_root"]) / row["path"]
    if not src.is_file():
        return None
    with open(src, "rb") as fh:
        fh.seek(row["start_byte"])
        raw = fh.read(min(row["end_byte"] - row["start_byte"], cap + 4))
    return clip(raw.decode("utf-8", "replace"), cap)


def render(c: Card, *, label: str | None = None, with_context: bool = True) -> str:
    """One card as prompt text. Lean is quoted verbatim inside a fenced block."""
    head = f"[{label}] " if label else ""
    lines = [f"{head}{c.kind} `{c.full_name}`  — {c.cite}", "```lean", c.signature, "```"]
    if c.docstring:
        lines.insert(1, f"docstring: {c.docstring}")
    if with_context and c.context:
        lines += ["active context (open/variable):", "```lean", c.context, "```"]
    return "\n".join(lines)
