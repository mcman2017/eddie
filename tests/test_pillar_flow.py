"""Directed pillar flow: exact chapter order, tiers, net flows, and confident timeline matches."""

from __future__ import annotations

from collections import Counter
from itertools import permutations

import numpy as np

from eddie.planner.pillars import (
    PillarSpec,
    TimelineItem,
    flow_order,
    forward_weight,
    match_timeline,
    net_flows,
    pillar_flow,
    timeline_items,
)


def chain(n: int) -> np.ndarray:
    """Pillar 0 is the core and builds on n-1, which builds on n-2, ... down to pillar 1."""
    w = np.zeros((n, n), dtype=np.int64)
    w[0, n - 1] = 5
    for p in range(n - 1, 1, -1):
        w[p, p - 1] = 3
    return w


def test_a_dag_gets_a_topological_order_with_the_core_last():
    w = chain(6)
    order, exact = flow_order(w)
    assert exact and order == [1, 2, 3, 4, 5, 0]
    assert forward_weight(w, order) == (int(w.sum()), int(w.sum()))


def test_exact_order_matches_brute_force_on_a_cyclic_graph():
    rng = np.random.default_rng(3)
    w = rng.integers(0, 40, size=(7, 7))
    np.fill_diagonal(w, 0)
    order, _ = flow_order(w)
    assert order[-1] == 0
    best = max(forward_weight(w, [*perm, 0])[0] for perm in permutations(range(1, 7)))
    assert forward_weight(w, order)[0] == best


def test_order_is_deterministic_on_ties():
    w = np.zeros((5, 5), dtype=np.int64)  # no cross references at all: every order is optimal
    assert flow_order(w)[0] == flow_order(w)[0]
    assert sorted(flow_order(w)[0]) == [0, 1, 2, 3, 4]


def test_tiers_follow_net_direction_and_dominators():
    w = np.zeros((5, 5), dtype=np.int64)
    w[2, 1] = 40  # 2 builds on 1
    w[3, 1] = 60
    w[3, 2] = 30
    w[4, 2] = 20
    w[4, 3] = 50
    w[0, 4] = 2
    flow = pillar_flow(w, {4: 0.3, 2: 0.4})
    assert flow.tier[0] == "main" and flow.order[-1] == 0
    assert flow.tier[1] == "foundations"  # only ever built on
    assert flow.tier[4] == "bottleneck"  # a net consumer holding a 30% dominator
    assert flow.tier[3] == "machinery"  # a net consumer without one
    assert flow.tier[2] == "machinery"  # a big dominator, but a (mild) net provider
    assert [flow.tier[p] for p in flow.order] == ["foundations", "machinery", "machinery", "bottleneck", "main"]
    assert flow.rank[1] == 0 and flow.forward == flow.total == int(w.sum())
    assert flow.ref_in[1] == 100 and flow.ref_out[3] == 90


def test_net_flows_point_from_provider_to_consumer():
    w = np.zeros((3, 3), dtype=np.int64)
    w[2, 1] = 70  # 2 builds on 1 ...
    w[1, 2] = 20  # ... more than 1 builds on 2
    w[0, 2] = 4
    flows = net_flows(w, [1, 2, 0])
    assert flows[0] == {"provider": 1, "consumer": 2, "net": 50, "forward": 70, "backward": 20}
    assert flows[1] == {"provider": 2, "consumer": 0, "net": 4, "forward": 4, "backward": 0}


def _specs(n: int) -> list[PillarSpec]:
    return [PillarSpec(prefixes=[f"P{i}"], title=f"Pillar {i}", category="X", live=10) for i in range(n)]


def _item(number: int, phrase: str, body: str = "") -> TimelineItem:
    return timeline_items(f"1. filler one.\n2. filler two.\n3. filler three.\n{number}. {phrase}. {body}\n")[-1]


def test_an_ambiguous_step_stays_unplaced():
    specs = _specs(4)
    vocab = [Counter(), Counter({"tower": 30}), Counter({"tower": 28}), Counter({"slope": 9})]
    item = TimelineItem(4, "Iwasawa tower", "Iwasawa tower", ["iwasawa", "tower"])
    (m,) = match_timeline(specs, vocab, [item])
    assert m.pillar is None and m.candidate == 1 and m.margin < 1.5
    assert not specs[1].timeline and not specs[2].timeline


def test_a_clear_title_match_is_placed():
    specs = _specs(4)
    vocab = [Counter(), Counter({"tower": 30, "iwasawa": 30}), Counter({"tower": 2, "norm": 40}), Counter({"slope": 9})]
    item = TimelineItem(4, "Iwasawa tower", "Iwasawa tower", ["iwasawa", "tower"])
    (m,) = match_timeline(specs, vocab, [item])
    assert m.pillar == 1 and m.margin >= 1.5
    assert [i.number for i in specs[1].timeline] == [4] and "iwasawa" in specs[1].keywords


def test_a_match_only_in_the_paragraph_body_is_not_a_title_match():
    specs = _specs(3)
    vocab = [Counter(), Counter({"vanishing": 30}), Counter({"slope": 9})]
    item = TimelineItem(4, "GRH", "GRH. vanishing of a number of groups follows", ["grh", "vanishing", "number", "group"])
    (m,) = match_timeline(specs, vocab, [item])
    assert m.pillar is None and m.candidate == 1 and m.score > 0


def test_a_garbled_title_is_never_placed():
    specs = _specs(3)
    vocab = [Counter(), Counter({"classe": 30}), Counter({"slope": 9})]
    garbled = TimelineItem(4, "for the classes in H 1 ( oE", "raw", ["classe"])
    fallback = TimelineItem(5, "Step 5", "raw", ["classe"])
    assert not garbled.clean and not fallback.clean
    assert [m.pillar for m in match_timeline(specs, vocab, [garbled, fallback])] == [None, None]


def test_timeline_item_helper_builds_real_items():
    item = _item(4, "Bost slope inequality")
    assert item.number == 4 and item.clean and {"slope", "inequality"} <= item.phrase_words


def test_a_title_word_every_pillar_uses_is_not_evidence():
    specs = _specs(6)
    vocab = [Counter(), *[Counter({"lift": 5}) for _ in range(5)]]
    vocab[1].update({"twisting": 40})  # the clear winner, but only through the paragraph body
    item = TimelineItem(4, "cost of each lift", "cost of each lift. twisting", ["cost", "lift", "twisting"])
    (m,) = match_timeline(specs, vocab, [item])
    assert m.candidate == 1 and m.margin >= 1.5 and m.pillar is None
