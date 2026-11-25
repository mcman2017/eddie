"""Strict parsing of Ember `usage` blocks.

A settlement must never under-charge silently. The parser returns three outcomes:

* parse_error: required fields are missing or malformed (e.g. OpenAI-style names). The
  call is charged its full reservation.
* hard anomalies: invariants are violated. The call is charged max(reservation, computed)
  and the breaker trips for human review.
* warnings: benign extras, such as unknown counters inside `*_tokens_details`. These are
  logged and stored, but do not trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eddie.config import TIER_BOUNDARY_INPUT_TOKENS
from eddie.llm import pricing
from eddie.llm.types import Usage

KNOWN_TOP_LEVEL = frozenset(
    {"input_tokens", "input_tokens_details", "output_tokens", "output_tokens_details", "total_tokens"}
)
_KNOWN_DETAILS = {"input_tokens_details": "cached_tokens", "output_tokens_details": "reasoning_tokens"}


@dataclass(frozen=True)
class UsageCheck:
    usage: Usage | None
    cost_nano: int | None
    hard: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    parse_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.parse_error is None and not self.hard


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def evaluate_usage(
    raw: Any, *, est_input_tokens: int, max_output_tokens: int, reserved_nano: int
) -> UsageCheck:
    if not isinstance(raw, dict):
        return UsageCheck(None, None, parse_error="usage block missing or not an object")

    hard: list[str] = []
    warnings: list[str] = []
    extra: dict[str, Any] = {}

    unknown = sorted(set(raw) - KNOWN_TOP_LEVEL)
    if unknown:
        hard.append(f"unknown top-level usage keys: {unknown}")

    input_tokens, output_tokens = raw.get("input_tokens"), raw.get("output_tokens")
    if not _is_int(input_tokens) or not _is_int(output_tokens):
        return UsageCheck(
            None,
            None,
            hard=tuple(hard),
            parse_error="usage.input_tokens / usage.output_tokens missing or not integers",
        )

    sub_values: dict[str, int] = {}
    for section, known_key in _KNOWN_DETAILS.items():
        details = raw.get(section)
        if details is None:
            details = {}
        if not isinstance(details, dict):
            hard.append(f"usage.{section} is not an object")
            details = {}
        value = details.get(known_key)
        if value is None:
            warnings.append(f"usage.{section}.{known_key} missing (treated as 0, i.e. billed uncached)")
            value = 0
        elif not _is_int(value):
            hard.append(f"usage.{section}.{known_key} is not an integer")
            value = 0
        sub_values[known_key] = value
        for key, other in details.items():
            if key == known_key:
                continue
            extra[f"{section}.{key}"] = other
            warnings.append(f"unrecognized usage counter {section}.{key}={other!r}")
            if _is_int(other) and other < 0:
                hard.append(f"negative usage counter {section}.{key}")

    cached, reasoning = sub_values["cached_tokens"], sub_values["reasoning_tokens"]
    for label, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("cached_tokens", cached),
        ("reasoning_tokens", reasoning),
    ):
        if value < 0:
            hard.append(f"negative {label}")

    total = raw.get("total_tokens")
    if total is not None and (not _is_int(total) or total != input_tokens + output_tokens):
        warnings.append("usage.total_tokens != input_tokens + output_tokens")

    if cached > input_tokens:
        hard.append("cached_tokens > input_tokens")
    if reasoning > output_tokens:
        hard.append("reasoning_tokens > output_tokens")
    if output_tokens > max_output_tokens:
        hard.append(f"output_tokens {output_tokens} > max_output_tokens {max_output_tokens}")
    if input_tokens > est_input_tokens:
        hard.append(f"input_tokens {input_tokens} > upper-bound estimate {est_input_tokens}")
    if input_tokens > TIER_BOUNDARY_INPUT_TOKENS:
        hard.append(f"input_tokens {input_tokens} > {TIER_BOUNDARY_INPUT_TOKENS}: billed at the 3x tier")

    safe_in, safe_out = max(input_tokens, 0), max(output_tokens, 0)
    safe_cached = min(max(cached, 0), safe_in)
    cost = pricing.cost_nano(safe_in, safe_cached, safe_out)
    if cost > reserved_nano:
        hard.append(f"actual cost {cost} nUSD exceeds reservation {reserved_nano} nUSD")

    usage = Usage(
        input_tokens=input_tokens,
        cached_tokens=cached,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        extra=extra,
    )
    return UsageCheck(usage, cost, tuple(hard), tuple(warnings), None, extra)
