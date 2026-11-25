"""Bounded fan-out for the zoom tree.

The archive keeps tens of thousands of generated modules in single directories (one holds
42,424). A map that shows a directory's children directly would draw a few hundred dots and
a "+42,000 more" void, so wide nodes get synthetic group nodes instead:

1. children are bucketed by the next CamelCase word of their name ("Exists", "Theta", ...);
2. if there are at most `fanout` buckets, each multi-member bucket becomes a prefix group
   ("Exists*", read as "names starting with Exists") and singletons stay direct children;
3. otherwise consecutive buckets are merged into balanced alphabetical ranges
   ("Abelian – Arith"), and any bucket bigger than a range stays a prefix group of its own;
4. every group recurses until no node has more than `fanout` children.

The result is deterministic and depends only on the names.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass, field

WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
FANOUT = 96
MAX_DEPTH = 8


@dataclass
class Group:
    label: str
    members: list[Entry] = field(default_factory=list)


Entry = Group | Hashable
Item = tuple[Hashable, str]


def words(label: str) -> list[str]:
    found = WORD.findall(label)
    return found or [label]


def _prefix(label: str, depth: int) -> tuple[str, ...]:
    return tuple(words(label)[: depth + 1])


def _range_label(first: tuple[str, ...], last: tuple[str, ...]) -> str:
    a, b = "".join(first), "".join(last)
    return f"{a}*" if a == b else f"{a} – {b}"


def _chunk(entries: list[tuple[str, Entry]], fanout: int) -> list[Entry]:
    """Fold an ordered list of (sort key, entry) into at most `fanout` alphabetical ranges."""
    while len(entries) > fanout:
        size = math.ceil(len(entries) / fanout)
        folded: list[tuple[str, Entry]] = []
        for i in range(0, len(entries), size):
            part = entries[i : i + size]
            if len(part) == 1:
                folded.append(part[0])
                continue
            first, last = part[0][0], part[-1][0]
            folded.append((first, Group(f"{first} – {last}" if first != last else f"{first}*", [e for _, e in part])))
        entries = folded
    return [e for _, e in entries]


def plan_groups(items: list[Item], fanout: int = FANOUT, depth: int = 0) -> list[Entry]:
    """Arrange (key, label) items into at most `fanout` entries per level."""
    if fanout < 2:
        raise ValueError("fanout must be at least 2")
    items = sorted(items, key=lambda kv: (kv[1], str(kv[0])))
    if len(items) <= fanout:
        return [k for k, _ in items]
    if depth >= MAX_DEPTH:
        return _chunk([(label, key) for key, label in items], fanout)

    buckets: dict[tuple[str, ...], list[Item]] = defaultdict(list)
    for key, label in items:
        buckets[_prefix(label, depth)].append((key, label))
    if len(buckets) == 1:
        if all(len(words(label)) <= depth + 1 for _, label in items):
            return _chunk([(label, key) for key, label in items], fanout)
        return plan_groups(items, fanout, depth + 1)

    ordered = sorted(buckets.items())

    def as_entry(prefix: tuple[str, ...], members: list[Item]) -> Entry:
        if len(members) == 1:
            return members[0][0]
        return Group("".join(prefix) + "*", plan_groups(members, fanout, depth + 1))

    if len(ordered) <= fanout:
        return [as_entry(prefix, members) for prefix, members in ordered]

    target = math.ceil(len(items) / fanout)
    entries: list[tuple[str, Entry]] = []
    run: list[tuple[tuple[str, ...], list[Item]]] = []
    run_size = 0

    def close_run() -> None:
        nonlocal run, run_size
        if not run:
            return
        if len(run) == 1:
            prefix, members = run[0]
            entries.append(("".join(prefix), as_entry(prefix, members)))
        else:
            merged = [m for _, members in run for m in members]
            entries.append((
                "".join(run[0][0]),
                Group(_range_label(run[0][0], run[-1][0]), plan_groups(merged, fanout, depth)),
            ))
        run, run_size = [], 0

    for prefix, members in ordered:
        if len(members) >= target:
            close_run()
            entries.append(("".join(prefix), as_entry(prefix, members)))
            continue
        if run and run_size + len(members) > target:
            close_run()
        run.append((prefix, members))
        run_size += len(members)
    close_run()
    return _chunk(entries, fanout)


def max_fanout(entries: list[Entry]) -> int:
    """The widest level in a plan (for tests and sanity checks)."""
    widest = len(entries)
    for e in entries:
        if isinstance(e, Group):
            widest = max(widest, max_fanout(e.members))
    return widest


def leaves(entries: list[Entry]) -> list[Hashable]:
    out: list[Hashable] = []
    for e in entries:
        if isinstance(e, Group):
            out.extend(leaves(e.members))
        else:
            out.append(e)
    return out
