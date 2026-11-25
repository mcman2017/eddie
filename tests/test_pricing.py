import pytest

from eddie.llm import pricing


def test_tier_one_exact_cost():
    # 100K uncached input + 10K output = $0.01 + $0.005
    assert pricing.cost_nano(100_000, 0, 10_000) == 15_000_000


def test_tier_boundary_is_inclusive_at_200k():
    assert pricing.cost_nano(200_000, 0, 0) == 200_000 * 100
    assert pricing.tier_for(200_000) is pricing.TIER_1


def test_whole_request_jumps_to_tier_two_above_200k():
    assert pricing.tier_for(200_001) is pricing.TIER_2
    assert pricing.cost_nano(200_001, 0, 1_000) == 200_001 * 300 + 1_000 * 1_500


def test_cached_tokens_count_toward_the_tier_and_are_a_subset():
    # 200,001 input of which 200,001 cached is still tier 2 (cached included in the tier test)
    assert pricing.cost_nano(200_001, 200_001, 0) == 200_001 * 12
    # subset: 1,000 input with 400 cached bills 600 uncached + 400 cached
    assert pricing.cost_nano(1_000, 400, 0) == 600 * 100 + 400 * 4


def test_reasoning_tokens_are_never_added_on_top_of_output():
    # cost() has no reasoning parameter at all: output_tokens already includes reasoning
    assert pricing.cost_nano(0, 0, 1_000) == 500_000


@pytest.mark.parametrize(
    "args", [(-1, 0, 0), (0, -1, 0), (0, 0, -1), (10, 11, 0), (True, 0, 0), (1.5, 0, 0)]
)
def test_invalid_counts_raise(args):
    with pytest.raises(ValueError):
        pricing.cost_nano(*args)


def test_reservation_assumes_no_cache_and_full_output():
    assert pricing.reserve_nano(1_000, 16) == 1_000 * 100 + 16 * 500


def test_worst_case_w_is_bounded_by_the_context_window():
    assert pricing.worst_case_w_nano(16) == (1_048_576 - 16) * 300 + 16 * 1_500 == 314_592_000
    assert pricing.worst_case_w_nano(32_768) == 353_894_400
    # W always dominates any bounded reservation the gateway can make (est <= 190K)
    assert pricing.worst_case_w_nano(32_768) > pricing.reserve_nano(190_000, 32_768)


def test_integer_math_is_exact_at_scale():
    # 8 dollars of output exactly: 16M output tokens at $0.50/1M
    assert pricing.cost_nano(0, 0, 16_000_000) == 8_000_000_000
