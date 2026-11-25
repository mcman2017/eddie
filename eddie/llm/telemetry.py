"""Proof-of-usage export: `eddie_ember_telemetry.json`.

One record per billable Ember call (settled or orphan-charged), in settlement order, with
a running cumulative spend. That spend includes recorded adjustments (opening balance,
manual entries), so the final cumulative figure equals the governor's committed total.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Any

from eddie.config import EMBER_MODEL, NANO_PER_USD
from eddie.llm.ledger import Ledger

SCHEMA = "eddie-ember-telemetry/1"


def _iso(ts: float | None) -> str | None:
    return None if ts is None else dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat(timespec="milliseconds")


def _usd(nano: int) -> float:
    return round(nano / NANO_PER_USD, 9)


def build_trace(
    ledger: Ledger, *, lifetime_cap_nano: int | None = None, shadow: Ledger | None = None
) -> dict[str, Any]:
    events: list[tuple[float, int, str, Any]] = []
    for adj in ledger.adjustments():
        events.append((adj["created_at"], 0, "adjustment", adj))
    for row in ledger.charged_calls():
        events.append((row["finished_at"] or row["created_at"], 1, "call", row))
    events.sort(key=lambda e: (e[0], e[1]))

    cumulative = 0
    calls_nano = 0
    calls: list[dict[str, Any]] = []
    adjustments: list[dict[str, Any]] = []
    totals = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    for ts, _, kind, row in events:
        if kind == "adjustment":
            cumulative += row["nano"]
            adjustments.append(
                {
                    "timestamp": _iso(ts),
                    "kind": row["kind"],
                    "cost_usd": _usd(row["nano"]),
                    "note": row["note"],
                    "cumulative_spend_usd": _usd(cumulative),
                }
            )
            continue
        cumulative += row["actual_nano"]
        calls_nano += row["actual_nano"]
        for key in totals:
            totals[key] += row[key] or 0
        calls.append(
            {
                "timestamp": _iso(ts),
                "call_id": row["id"],
                "response_id": row["response_id"],
                "brain_region": row["region"],
                "effort": row["effort"],
                "input_tokens": row["input_tokens"],
                "cached_tokens": row["cached_tokens"],
                "output_tokens": row["output_tokens"],
                "reasoning_tokens": row["reasoning_tokens"],
                "cost_usd": _usd(row["actual_nano"]),
                "cumulative_spend_usd": _usd(cumulative),
                "state": row["state"],
                "status": row["status"],
                "http_status": row["http_status"],
                "latency_ms": row["latency_ms"],
                "tag": row["tag"],
            }
        )

    trace: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": _iso(dt.datetime.now(tz=dt.UTC).timestamp()),
        "model": EMBER_MODEL,
        "lifetime_cap_usd": _usd(lifetime_cap_nano) if lifetime_cap_nano is not None else None,
        "totals": {
            "calls": len(calls),
            **totals,
            "calls_cost_usd": _usd(calls_nano),
            "adjustments_usd": _usd(cumulative - calls_nano),
            "total_spend_usd": _usd(cumulative),
        },
        "calls": calls,
        "adjustments": adjustments,
    }
    if shadow is not None:
        trace["shadow_calls"] = [
            {
                "timestamp": _iso(r["finished_at"]),
                "backend": r["backend"],
                "brain_region": r["region"],
                "effort": r["effort"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "reasoning_tokens": r["reasoning_tokens"],
                "would_have_cost_usd": _usd(r["shadow_nano"] or 0),
            }
            for r in shadow.charged_calls(include_nonbillable=True)
        ]
    return trace


def write_json_atomic(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path
