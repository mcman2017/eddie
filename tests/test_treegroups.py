"""Bounded fan-out grouping for the zoom tree."""

import random

import pytest

from eddie.ingest.treegroups import Group, leaves, max_fanout, plan_groups, words


def test_words_split_camel_case_and_digits():
    assert words("ExistsUniqueHPrime2Foo") == ["Exists", "Unique", "H", "Prime", "2", "Foo"]
    assert words("Block Decomposition") == ["Block", "Decomposition"]
    assert words("___") == ["___"]


def test_small_levels_are_left_alone():
    items = [(i, f"Name{i}") for i in range(10)]
    assert plan_groups(items, fanout=16) == sorted(range(10), key=lambda i: f"Name{i}")


def test_prefix_groups_and_singletons():
    items = [(f"e{i}", f"Exists{w}") for i, w in enumerate(["Alpha", "Beta", "Gamma", "Delta"])]
    items += [("t1", "ThetaLift"), ("t2", "ThetaSeries"), ("z", "Zeta")]
    plan = plan_groups(items, fanout=4)
    labels = [e.label if isinstance(e, Group) else e for e in plan]
    assert labels == ["Exists*", "Theta*", "z"]
    assert sorted(leaves(plan)) == sorted(k for k, _ in items)


@pytest.mark.parametrize("n,fanout", [(5000, 96), (42_424, 96), (700, 8)])
def test_wide_flat_directories_are_bounded_and_lossless(n, fanout):
    rng = random.Random(n)
    vocab = ["Exists", "Is", "Theta", "Finite", "Local", "Flag", "Integral", "Mem", "Weil"] + [
        "".join(rng.choice("BCDFGKLMNPRSTV") + rng.choice("aeiou") for _ in range(3)).capitalize() for _ in range(1500)
    ]
    items = []
    for i in range(n):
        head = "Exists" if i % 6 == 0 else rng.choice(vocab)
        items.append((i, head + rng.choice(vocab) + rng.choice(vocab) + str(i)))
    plan = plan_groups(items, fanout=fanout)
    assert max_fanout(plan) <= fanout
    assert sorted(leaves(plan)) == list(range(n))


def test_identical_prefixes_fall_back_to_alphabetical_ranges():
    items = [(i, "Same") for i in range(50)]
    plan = plan_groups(items, fanout=8)
    assert max_fanout(plan) <= 8
    assert sorted(leaves(plan)) == list(range(50))


def test_deterministic():
    items = [(i, f"Word{i % 37}Tail{i}") for i in range(3000)]
    a = plan_groups(list(items), fanout=20)
    b = plan_groups(list(reversed(items)), fanout=20)
    assert repr(a) == repr(b)
