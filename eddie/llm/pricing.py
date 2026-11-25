"""`muse-ember-1.0` pricing in exact integer nano-USD per token.

| tier            | uncached input | cached input | output (visible + reasoning) |
|-----------------|----------------|--------------|------------------------------|
| <= 200K input   | $0.10 / 1M     | $0.004 / 1M  | $0.50 / 1M                   |
| > 200K input    | $0.30 / 1M     | $0.012 / 1M  | $1.50 / 1M                   |

The tier is decided by total input tokens (cached included) and applies to the WHOLE
request. `cached_tokens` is a subset of `input_tokens` and `reasoning_tokens` is a subset
of `output_tokens`: neither is ever added on top.
"""

from __future__ import annotations

from dataclasses import dataclass

from eddie.config import CONTEXT_WINDOW_TOKENS, TIER_BOUNDARY_INPUT_TOKENS


@dataclass(frozen=True)
class PriceTier:
    name: str
    input_nano: int  # per uncached input token
    cached_nano: int  # per cached input token
    output_nano: int  # per output token (reasoning included)


TIER_1 = PriceTier("<=200K", input_nano=100, cached_nano=4, output_nano=500)
TIER_2 = PriceTier(">200K", input_nano=300, cached_nano=12, output_nano=1_500)


def tier_for(input_tokens: int) -> PriceTier:
    return TIER_2 if input_tokens > TIER_BOUNDARY_INPUT_TOKENS else TIER_1


def cost_nano(input_tokens: int, cached_tokens: int, output_tokens: int) -> int:
    for name, value in (("input", input_tokens), ("cached", cached_tokens), ("output", output_tokens)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} tokens must be a non-negative int, got {value!r}")
    if cached_tokens > input_tokens:
        raise ValueError("cached tokens cannot exceed input tokens")
    t = tier_for(input_tokens)
    return (input_tokens - cached_tokens) * t.input_nano + cached_tokens * t.cached_nano + output_tokens * t.output_nano


def reserve_nano(est_input_tokens: int, max_output_tokens: int) -> int:
    """Worst case for a bounded estimate: no cache hits, every output token used."""
    t = tier_for(est_input_tokens)
    return est_input_tokens * t.input_nano + max_output_tokens * t.output_nano


def worst_case_w_nano(max_output_tokens: int) -> int:
    """W(m): the most a single request can cost, independent of any estimate.

    input + max_output must fit in the context window (else HTTP 400), so the input can
    be at most (window - m) tokens, all billed at tier-2 uncached rates.
    """
    m = max_output_tokens
    if m < 0 or m > CONTEXT_WINDOW_TOKENS:
        raise ValueError("max_output_tokens out of range")
    return (CONTEXT_WINDOW_TOKENS - m) * TIER_2.input_nano + m * TIER_2.output_nano
