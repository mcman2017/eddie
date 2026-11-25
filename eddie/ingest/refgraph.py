"""Declaration-level reference extraction and name resolution ($0, local).

Each declaration's text (signature and body) is scanned in the lexer's masked copy, so
comments and strings never count. Every identifier is resolved against the project's
own declarations the way Lean resolves names, first hit wins:

1. the namespace chain of the declaration: `A.B.C.t`, `A.B.t`, `A.t`, `t`
   (the namespace of a dotted declaration name counts too);
2. each `open`ed namespace in scope, including `open … in` prefixes;
3. `_root_.t` and the explicit form `@t`;
4. generalized field notation: the longest dotted prefix naming a constant wins
   (`c.trans` -> `c`); on a local (`h.foo`) the lowercase head is dropped and the rest resolved;
5. last resort: a unique last component of at least 6 characters, flagged *weak*.

Names outside the project (Mathlib, core) are ignored: the graph is project-local.
"""

from __future__ import annotations

import os
import re
import sqlite3
from array import array
from collections import Counter
from dataclasses import dataclass

from eddie.ingest.lean_parser import parse_source_with_code

_COMPONENT = r"(?:«[^»\n]*»|[^\W\d][\w'!?]*)"
IDENT = re.compile(rf"(?<![\w.'@])@?{_COMPONENT}(?:\.{_COMPONENT})*")
_OPEN_ITEM = re.compile(r"^open\s+(.*?)(?:\s+in)?$")
KEYWORDS = frozenset(
    """by fun λ have show from at with then else if let in do match return exact intro intros apply
    refine simp rw rfl calc using only this sorry obtain rcases rintro use constructor cases induction
    unfold omega linarith nlinarith positivity norm_num ring field_simp aesop decide trivial assumption
    contradiction exfalso left right ext funext congr gcongr specialize generalize subst change at
    theorem lemma def abbrev instance structure class inductive axiom where deriving open namespace
    section end variable universe noncomputable private protected public Type Prop Sort true false""".split()
)
WEAK_MIN_LEN = 6


def parse_opens(context: str | None) -> list[str]:
    """Namespaces opened in a declaration's scope (`open A B`, `open scoped C`, `open D in`)."""
    if not context:
        return []
    opens: list[str] = []
    for item in context.split("; "):
        m = _OPEN_ITEM.match(item.strip())
        if not m:
            continue
        for tok in m.group(1).split():
            if tok in ("scoped", "hiding", "renaming", "→") or tok.startswith("("):
                if tok in ("hiding", "renaming") or tok.startswith("("):
                    break
                continue
            opens.append(tok.rstrip(")"))
    return opens


@dataclass
class NameTable:
    by_name: dict[str, int]
    unique_last: dict[str, int]

    @classmethod
    def from_pairs(cls, pairs) -> NameTable:
        by_name: dict[str, int] = {}
        last_counts: Counter[str] = Counter()
        last_id: dict[str, int] = {}
        for full_name, decl_id in pairs:
            if not full_name:
                continue
            by_name.setdefault(full_name, decl_id)
            last = full_name.rsplit(".", 1)[-1]
            last_counts[last] += 1
            last_id[last] = decl_id
        unique_last = {k: last_id[k] for k, c in last_counts.items() if c == 1 and len(k) >= WEAK_MIN_LEN}
        return cls(by_name, unique_last)

    @classmethod
    def from_index(cls, index_path: str) -> NameTable:
        con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            return cls.from_pairs(con.execute("SELECT full_name, id FROM decls ORDER BY id"))
        finally:
            con.close()


def resolve(token: str, ns_parts: list[str], opens: list[str], table: NameTable) -> tuple[int | None, bool]:
    """Return (decl_id, weak) for one identifier token, or (None, False)."""
    if token.startswith("@"):
        token = token[1:]
    if token in KEYWORDS:
        return None, False
    by_name = table.by_name
    if token.startswith("_root_."):
        token = token[len("_root_.") :]
        ns_parts, opens = [], []
    parts = token.split(".")
    # longest prefix naming a constant wins; the rest is generalized field notation (`c.trans`)
    for k in range(len(parts), 0, -1):
        head = ".".join(parts[:k])
        for i in range(len(ns_parts), -1, -1):
            hit = by_name.get(".".join([*ns_parts[:i], head]) if i else head)
            if hit is not None:
                return hit, False
        for ns in opens:
            hit = by_name.get(f"{ns}.{head}")
            if hit is not None:
                return hit, False
    if "." in token:
        head, rest = token.split(".", 1)
        if head[:1].islower():
            hit, weak = resolve(rest, ns_parts, opens, table)
            if hit is not None:
                return hit, weak
    last = token.rsplit(".", 1)[-1]
    hit = table.unique_last.get(last)
    if hit is not None:
        return hit, True
    return None, False


def decl_edges(code: str, start: int, end: int, ns_parts: list[str], opens: list[str], table: NameTable, self_id: int):
    """Resolved (dst_id, weak) pairs for one declaration's text span, deduplicated."""
    seen_tokens: set[str] = set()
    out: dict[int, bool] = {}
    for m in IDENT.finditer(code, start, end):
        tok = m.group()
        if tok in seen_tokens:
            continue
        seen_tokens.add(tok)
        dst, weak = resolve(tok, ns_parts, opens, table)
        if dst is None or dst == self_id:
            continue
        if dst not in out or (out[dst] and not weak):
            out[dst] = weak
    return out.items()


# --- process-pool worker ---------------------------------------------------------------------

_TABLE: NameTable | None = None


def init_worker(index_path: str) -> None:
    global _TABLE
    _TABLE = NameTable.from_index(index_path)


def module_edges(args: tuple[int, str, str, int, int]) -> tuple[int, bytes, bytes, bytes, str | None]:
    """Worker: edges for every declaration of one module (ids are contiguous per module)."""
    module_id, source_root, rel_path, first_id, n_decls = args
    assert _TABLE is not None
    with open(os.path.join(source_root, rel_path), "rb") as fh:
        record, code = parse_source_with_code(rel_path, fh.read())
    if len(record.decls) != n_decls:
        return module_id, b"", b"", b"", f"{rel_path}: parsed {len(record.decls)} decls, index has {n_decls}"
    src, dst, weak = array("I"), array("I"), array("B")
    for i, d in enumerate(record.decls):
        self_id = first_id + i
        if d.full_name:
            ns_parts = d.full_name.split(".")[:-1]
        else:
            ns_parts = d.namespace.split(".") if d.namespace else []
        for target, is_weak in decl_edges(code, d.start_char, d.end_char, ns_parts, parse_opens(d.context), _TABLE, self_id):
            src.append(self_id)
            dst.append(target)
            weak.append(1 if is_weak else 0)
    return module_id, src.tobytes(), dst.tobytes(), weak.tobytes(), None
