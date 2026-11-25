import pytest

from eddie.llm import pricing
from eddie.llm.usage import evaluate_usage

EST = 5_000
M = 512
RESERVED = pricing.reserve_nano(EST, M)


def _usage(**over):
    u = {
        "input_tokens": 300,
        "input_tokens_details": {"cached_tokens": 100},
        "output_tokens": 200,
        "output_tokens_details": {"reasoning_tokens": 150},
        "total_tokens": 500,
    }
    u.update(over)
    return u


def _eval(u, est=EST, m=M, reserved=RESERVED):
    return evaluate_usage(u, est_input_tokens=est, max_output_tokens=m, reserved_nano=reserved)


def test_valid_usage_settles_exactly():
    r = _eval(_usage())
    assert r.ok
    assert r.cost_nano == 200 * 100 + 100 * 4 + 200 * 500
    assert r.usage.reasoning_tokens == 150


@pytest.mark.parametrize(
    "usage,needle",
    [
        (_usage(input_tokens=-1), "negative"),
        (_usage(output_tokens_details={"reasoning_tokens": -5}), "negative"),
        (_usage(input_tokens_details={"cached_tokens": 301}), "cached_tokens > input_tokens"),
        (_usage(output_tokens_details={"reasoning_tokens": 201}), "reasoning_tokens > output_tokens"),
        (_usage(output_tokens=M + 1, total_tokens=300 + M + 1), "max_output_tokens"),
        (_usage(input_tokens=EST + 1, total_tokens=EST + 201), "upper-bound estimate"),
        (_usage(billed_usd=1.0), "unknown top-level"),
    ],
)
def test_hard_anomalies(usage, needle):
    r = _eval(usage)
    assert not r.ok
    assert any(needle in h for h in r.hard), r.hard


def test_input_above_200k_trips_even_if_estimate_allowed_it():
    r = _eval(
        _usage(input_tokens=200_001, total_tokens=200_201, input_tokens_details={"cached_tokens": 0}),
        est=1_000_000,
        reserved=10**12,
    )
    assert any("3x tier" in h for h in r.hard)
    assert r.cost_nano == 200_001 * 300 + 200 * 1_500


def test_actual_above_reservation_trips():
    r = _eval(_usage(), reserved=1_000)
    assert any("exceeds reservation" in h for h in r.hard)


def test_extra_detail_counters_only_warn():
    u = _usage(
        input_tokens_details={"cached_tokens": 100, "image_tokens": 64, "audio_tokens": 0},
        output_tokens_details={"reasoning_tokens": 150, "accepted_prediction_tokens": 0},
    )
    r = _eval(u)
    assert r.ok, r.hard
    assert len(r.warnings) == 3
    assert r.extra["input_tokens_details.image_tokens"] == 64


def test_whitespace_heavy_prompt_far_above_ten_bytes_per_token_settles():
    # A tiny token count against a big byte estimate is legitimate (no lower bound).
    r = _eval(_usage(input_tokens=12, input_tokens_details={"cached_tokens": 0}, total_tokens=212), est=100_000)
    assert r.ok


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "lots",
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},  # OpenAI-style names
        {"input_tokens": 10},
        {"input_tokens": 10, "output_tokens": "5"},
        {"input_tokens": True, "output_tokens": 5},
    ],
)
def test_unparseable_usage_is_a_parse_error(usage):
    r = _eval(usage)
    assert r.parse_error is not None
    assert r.cost_nano is None


def test_missing_details_are_billed_as_uncached_with_warning():
    r = _eval({"input_tokens": 300, "output_tokens": 200})
    assert r.ok
    assert r.cost_nano == 300 * 100 + 200 * 500
    assert r.warnings
