"""Automatic Proof Pillar discovery.

Pillars are derived at runtime from the archive itself. Nothing about the archive or the
companion PDF is hard-coded here.

1. Pillar 0 is the top-level reduction: the root modules and the kernel they delegate to.
2. The live directory tree is split top-down. While there are too few pillars, or one
   holds too much of the live mass, the heaviest group is split into its children. Slivers
   fold into "<parent> (other)", and the result is clamped to K_MIN..K_MAX.
3. Titles are humanized directory names. When the companion PDF's numbered timeline has a
   step whose words match one pillar's module-path and declaration-name vocabulary clearly
   better than any other pillar's, that step becomes the pillar's subtitle; each step is
   used at most once, and a step that matches several pillars about equally stays unplaced.
4. Chapters follow the directed proof flow: pillars are ordered so that as much of the
   cross-pillar reference weight as possible points from an earlier pillar (the provider)
   to a later one (the consumer), in four acts ending at the top-level reduction.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

K_MIN, K_TARGET, K_MAX = 12, 16, 20
MAX_FRAC = 0.20
MIN_FRAC = 0.01
CORE_TOPS = ("Main", "Statements", "Supplements")

TIERS = ("foundations", "machinery", "bottleneck", "main")
FOUNDATION_NET = 1 / 3  # (in - out) / (in + out) at or above this: other pillars mostly build on it
BOTTLENECK_DOM = 0.01  # one theorem dominating at least this share of the live cone
EXACT_LIMIT = 20  # non-core pillars the exact subset search handles (2^20 states)
TIMELINE_MIN_SCORE = 0.02
TIMELINE_MIN_MARGIN = 1.5  # the best pillar must beat the runner-up by this factor
TIMELINE_MAX_DF = 0.8  # a title word used by more than this share of the pillars proves nothing

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP = frozenset(
    """the a an and or of on in to for via with by from as at is are be we our its it this that these those one
    two all any each more most much very time like into over under up out off than then also using use used
    again some such only first second sufficient precise computation definitively switch interpret indeed
    values meaning desired containing gives proven order specifically choice""".split()
)
_ITEM = re.compile(r"^\s*(\d{1,2})\.\s+(\S.*)$", re.M)
_DATE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\+?\s*:?\s*")


def camel_words(text: str) -> list[str]:
    return [w.lower() for w in _CAMEL.findall(text)]


def normalize_word(word: str) -> str:
    word = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode().lower()
    word = re.sub(r"[^a-z0-9]", "", word)
    for suffix, repl in (("ies", "y"), ("es", "e"), ("s", "")):
        if len(word) > 4 and word.endswith(suffix) and not word.endswith("ss"):
            return word[: -len(suffix)] + repl
    return word


def humanize(component: str) -> str:
    words = _CAMEL.findall(component)
    return " ".join(words) if words else component


# --- the companion PDF's numbered timeline --------------------------------------------------------


@dataclass
class TimelineItem:
    number: int
    phrase: str
    raw: str
    words: list[str]

    @property
    def clean(self) -> bool:
        """False for a fallback title or one with stray brackets (garbled math from the PDF)."""
        return self.phrase != f"Step {self.number}" and not re.search(r"[()\[\]{}]", self.phrase)

    @property
    def phrase_words(self) -> set[str]:
        return {normalize_word(w) for w in re.findall(r"[^\W\d_][\w'’-]+", self.phrase)}


@dataclass
class TimelineMatch:
    """How one timeline step scored against the pillars; `pillar` is set only when confident."""

    number: int
    pillar: int | None
    candidate: int | None
    score: float
    margin: float  # best score / runner-up score (99 when nothing else matched)


def extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


_MATHY = re.compile(r"[≡≤≥∑∏∫Λλ⊞→↦·()\[\]{}=+/^_|]|\d")
_TRAILING = frozenset("a an the to of for in on and or via with by at as from is are be d k p u x".split())


_CONNECTORS = _TRAILING | frozenset("using via over under into onto than then".split())


def _content_words(tokens: list[str]) -> list[str]:
    return [t for t in tokens if re.fullmatch(r"[^\W\d_][\w'’-]{2,}", t) and t.lower() not in _CONNECTORS]


def clean_phrase(phrase: str, rest: str = "", max_tokens: int = 8) -> str:
    """A short, camera-readable title.

    Keep up to 8 tokens; once there are 3 content words, math ends the title; trim dangling
    connectors and single characters. A title with no content word borrows up to two
    capitalized names or long technical terms from the rest of the step.
    """
    out: list[str] = []
    for tok in phrase.split():
        tok = tok.strip("\"'“”‘’,")
        if not tok:
            continue
        if len(out) >= max_tokens or (len(_content_words(out)) >= 3 and _MATHY.search(tok)):
            break
        out.append(tok)
    while out and (out[-1].lower() in _CONNECTORS or len(out[-1]) == 1 or out[-1] in "([{"):
        out.pop()
    title = " ".join(out).strip(" -–")
    if not _content_words(out):
        stems: set[str] = set()
        extra = []
        for w in re.findall(r"[^\W\d_][\w'’-]+", rest):
            if (w[0].isupper() and len(w) >= 4) or len(w) >= 8:
                if w.lower()[:6] not in stems and w.lower() not in _STOP:
                    stems.add(w.lower()[:6])
                    extra.append(w)
            if len(extra) == 2:
                break
        title = f"{title} — {', '.join(extra)}" if (title and extra) else (title or ", ".join(extra))
    if not _content_words(title.replace("—", " ").replace(",", " ").split()):
        return ""  # pure garbled math: the caller falls back to "Step N"
    return title[:80]


def timeline_items(text: str) -> list[TimelineItem]:
    """Numbered steps `N. [date:] phrase …`, keeping the longest run that starts at 1."""
    matches = list(_ITEM.finditer(text))
    items: dict[int, TimelineItem] = {}
    for i, m in enumerate(matches):
        number = int(m.group(1))
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), m.end() + 400)
        chunk = "".join(ch for ch in text[m.start(2) : end] if unicodedata.category(ch) not in ("Mn", "Cf"))
        raw = re.sub(r"\s+", " ", chunk).strip()[:400]
        body = _DATE.sub("", raw)
        body = re.sub(r"\b(i)\.(e)\.|\b(e)\.(g)\.", lambda mm: "".join(c for c in mm.groups() if c), body)
        pieces = re.split(r"(?<=[a-zA-Z)])[.;:](?:\s|$)|,\s(?=[a-z])", body, maxsplit=1)
        phrase = clean_phrase(pieces[0], pieces[1] if len(pieces) > 1 else "")
        words = [normalize_word(w) for w in re.findall(r"[^\W\d_][\w'’-]+", body)]
        words = [w for w in words if len(w) >= 3 and w not in _STOP]
        if number not in items:
            items[number] = TimelineItem(number, phrase or f"Step {number}", raw, words)
    run, n = [], 1
    while n in items:
        run.append(items[n])
        n += 1
    return run if len(run) >= 3 else []


# --- pillar partition -----------------------------------------------------------------------------------


@dataclass
class Node:
    path: tuple[str, ...]
    own: int = 0  # live decls in modules named exactly by `path`
    children: dict[str, Node] = field(default_factory=dict)
    _mass: int | None = None

    @property
    def mass(self) -> int:
        if self._mass is None:
            self._mass = self.own + sum(c.mass for c in self.children.values())
        return self._mass


@dataclass
class PillarSpec:
    prefixes: list[str]  # module-name prefixes; the longest matching prefix wins
    title: str
    category: str
    live: int
    exact: list[str] = field(default_factory=list)  # modules matched exactly (not by prefix)
    timeline: list[TimelineItem] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


def _build_trie(module_live: dict[str, int]) -> Node:
    root = Node(())
    for name, live in module_live.items():
        node = root
        for part in name.split("."):
            node = node.children.setdefault(part, Node((*node.path, part)))
        node.own += live
    return root


@dataclass
class Group:
    path: tuple[str, ...]
    mass: int
    node: Node | None  # None for an "(other)" bucket of leftovers
    extra: list[str] = field(default_factory=list)  # prefixes of slivers folded into this group

    @property
    def splittable(self) -> bool:
        return self.node is not None and sum(1 for c in self.node.children.values() if c.mass > 0) >= 2


def partition(module_live: dict[str, int]) -> list[PillarSpec]:
    """Split the live directory tree into about K_MIN..K_MAX pillars (pillar 0 = core)."""
    total = sum(module_live.values()) or 1
    trie = _build_trie({k: v for k, v in module_live.items() if v > 0})
    core_mass = sum(trie.children[t].mass for t in CORE_TOPS if t in trie.children)
    frontier = [Group(n.path, n.mass, n) for k, n in trie.children.items() if k not in CORE_TOPS and n.mass > 0]

    def order(groups: list[Group]) -> None:
        groups.sort(key=lambda g: (-g.mass, g.path, g.node is None))

    while frontier:
        order(frontier)
        too_big = frontier[0].mass > MAX_FRAC * total
        need_more = len(frontier) + 1 < K_TARGET
        if not (too_big or need_more):
            break
        if not too_big and len(frontier) + 1 >= K_MAX:
            break
        target = next((g for g in frontier if g.splittable), None)
        if target is None:
            break
        frontier.remove(target)
        kids = [c for c in target.node.children.values() if c.mass > 0]
        big = [c for c in kids if c.mass >= MIN_FRAC * total]
        leftover = target.node.own + sum(c.mass for c in kids if c.mass < MIN_FRAC * total)
        frontier.extend(Group(c.path, c.mass, c) for c in big)
        if leftover:
            frontier.append(Group(target.path, leftover, None))

    # fold slivers into the heaviest group sharing their top directory; clamp to K_MAX
    order(frontier)
    while frontier and (len(frontier) + 1 > K_MAX or (frontier[-1].mass < MIN_FRAC * total and len(frontier) + 1 > K_MIN)):
        sliver = frontier.pop()
        host = next((g for g in frontier if g.path[:1] == sliver.path[:1]), frontier[0] if frontier else None)
        if host is None:
            frontier.append(sliver)
            break
        host.mass += sliver.mass
        host.extra.append(".".join(sliver.path))
        host.extra.extend(sliver.extra)
        order(frontier)

    specs = [PillarSpec(prefixes=list(CORE_TOPS), title="Top-level reduction", category="Main", live=core_mass)]
    for g in frontier:
        parts = list(g.path)
        title = " · ".join(humanize(p) for p in parts[1:]) or humanize(parts[0])
        if g.node is None:
            title += " (other)"
        specs.append(
            PillarSpec(prefixes=[".".join(parts), *g.extra], title=title, category=humanize(parts[0]), live=g.mass)
        )
    return specs


def assign_modules(module_names: list[str], specs: list[PillarSpec]) -> list[int]:
    """Pillar index for each module (longest matching prefix wins; -1 = no live pillar)."""
    table: list[tuple[str, int]] = []
    for i, spec in enumerate(specs):
        for prefix in spec.prefixes:
            table.append((prefix, i))
    table.sort(key=lambda t: -len(t[0]))
    out = []
    for name in module_names:
        hit = -1  # no live pillar claims it: wholly pruned dead code
        for prefix, i in table:
            if name == prefix or name.startswith(prefix + "."):
                hit = i
                break
        out.append(hit)
    return out


def match_timeline(specs: list[PillarSpec], vocab: list[Counter], items: list[TimelineItem]) -> list[TimelineMatch]:
    """Attach a timeline step to a pillar only when the vocabulary match is unambiguous.

    Each step is scored against every pillar (tf-idf over shared words). It is placed when
    its best pillar scores at least TIMELINE_MIN_SCORE, beats the runner-up by
    TIMELINE_MIN_MARGIN, shares an informative word with the step's own title phrase (not
    only with the paragraph after it, and not a word nearly every pillar uses), and the
    title is not garbled. Everything else is returned as an unplaced candidate, so a guess
    is never shown as a chapter title.
    """
    if not items:
        return []
    df: Counter[str] = Counter()
    for bag in vocab:
        df.update(set(bag))
    n_pillars = len(vocab)
    best: dict[int, tuple[float, int, list[str], float]] = {}
    for item in items:
        ranked = []
        for p, bag in enumerate(vocab):
            if p == 0:
                continue
            total = sum(bag.values()) or 1
            score = 0.0
            hits = []
            for w in sorted(set(item.words)):
                if bag.get(w):
                    idf = math.log((1 + n_pillars) / (1 + df[w]))
                    score += (bag[w] / total) ** 0.5 * idf
                    hits.append(w)
            if score > 0:
                ranked.append((score, p, hits))
        ranked.sort(key=lambda t: (-t[0], t[1]))
        if ranked:
            runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
            margin = min(99.0, ranked[0][0] / runner_up) if runner_up > 0 else 99.0
            best[item.number] = (ranked[0][0], ranked[0][1], ranked[0][2], margin)

    by_number = {i.number: i for i in items}
    placed: dict[int, int] = {}
    per_pillar: Counter[int] = Counter()
    for number in sorted(best, key=lambda n: (-best[n][0], n)):
        score, p, hits, margin = best[number]
        item = by_number[number]
        informative = {w for w in hits if df[w] <= TIMELINE_MAX_DF * n_pillars}
        confident = (
            score >= TIMELINE_MIN_SCORE
            and margin >= TIMELINE_MIN_MARGIN
            and item.clean
            and bool(item.phrase_words & informative)
        )
        if not confident or per_pillar[p] >= 2:
            continue
        specs[p].timeline.append(item)
        specs[p].keywords = sorted(set(specs[p].keywords) | set(hits))
        placed[number] = p
        per_pillar[p] += 1
    out = []
    for item in items:
        score, p, _hits, margin = best.get(item.number, (0.0, None, [], 0.0))
        out.append(TimelineMatch(item.number, placed.get(item.number), p, round(score, 6), round(margin, 3)))
    return out


def pillar_vocabulary(module_names: list[str], module_pillar: list[int], module_live: list[int],
                      decl_names_by_pillar: dict[int, list[str]], n_pillars: int) -> list[Counter]:
    vocab = [Counter() for _ in range(n_pillars)]
    for name, p, live in zip(module_names, module_pillar, module_live, strict=True):
        if not live or p < 0:
            continue
        for part in name.split("."):
            for w in camel_words(part):
                w = normalize_word(w)
                if len(w) >= 3 and w not in _STOP:
                    vocab[p][w] += live
    for p, names in decl_names_by_pillar.items():
        if p < 0:
            continue
        for full in names:
            for w in camel_words(full.rsplit(".", 1)[-1].replace("_", " ")):
                w = normalize_word(w)
                if len(w) >= 3 and w not in _STOP:
                    vocab[p][w] += 1
    return vocab



# --- directed proof flow ----------------------------------------------------------------------------


@dataclass
class Flow:
    """Chapter order of the pillars. `weights[u][d]` counts references from pillar u to pillar d,
    so u builds on d and the proof flows from d (the provider) to u (the consumer)."""

    order: list[int]  # pillar ids, foundations first, the core last
    rank: dict[int, int]
    tier: dict[int, str]
    ref_in: dict[int, int]  # references other pillars make to this one
    ref_out: dict[int, int]  # references this pillar makes to others
    forward: int  # reference weight whose provider comes before its consumer
    total: int
    exact: bool

    @property
    def forward_frac(self) -> float:
        return self.forward / self.total if self.total else 1.0

    def net(self, p: int) -> float:
        both = self.ref_in[p] + self.ref_out[p]
        return (self.ref_in[p] - self.ref_out[p]) / both if both else 0.0


def _exact_order(w: np.ndarray) -> list[int]:
    """The linear order with the least reference weight pointing at a later pillar.

    Dynamic programming over subsets (minimum feedback arc set): `best[S]` is the least
    backward weight of any order of the pillars in S, and placing v last after the set T
    costs every reference from T to v. Exact, and deterministic on ties.
    """
    k = len(w)
    if k <= 1:
        return list(range(k))
    full = 1 << k
    masks = np.arange(full, dtype=np.int64)
    bits = ((masks[:, None] >> np.arange(k)) & 1).astype(np.int64)
    pop = bits.sum(axis=1)
    into = bits @ w  # into[T, v] = references from the pillars in T to v
    best = np.full(full, np.iinfo(np.int64).max, dtype=np.int64)
    best[0] = 0
    last = np.full(full, -1, dtype=np.int64)
    for layer in range(1, k + 1):
        sel = masks[pop == layer]
        for v in range(k):
            s = sel[((sel >> v) & 1) == 1]
            prev = s ^ (1 << v)
            cand = best[prev] + into[prev, v]
            better = cand < best[s]
            best[s[better]] = cand[better]
            last[s[better]] = v
    order: list[int] = []
    mask = full - 1
    while mask:
        v = int(last[mask])
        order.append(v)
        mask ^= 1 << v
    order.reverse()
    return order


def _greedy_order(w: np.ndarray) -> list[int]:
    """Fallback above EXACT_LIMIT pillars: most-used first, then adjacent swaps while they help."""
    k = len(w)
    remaining = set(range(k))
    order: list[int] = []
    while remaining:
        pick = max(sorted(remaining), key=lambda v: sum(int(w[u, v]) - int(w[v, u]) for u in remaining))
        order.append(pick)
        remaining.discard(pick)
    improved = True
    while improved:
        improved = False
        for i in range(k - 1):
            a, b = order[i], order[i + 1]
            if w[a, b] > w[b, a]:  # a (earlier) builds on b (later): swapping removes more than it adds
                order[i], order[i + 1] = b, a
                improved = True
    return order


def flow_order(weights, core: int = 0) -> tuple[list[int], bool]:
    """Pillar ids from the most foundational to the core (pinned last), and whether it is exact."""
    w = np.asarray(weights, dtype=np.int64)
    n = len(w)
    ids = [p for p in range(n) if p != core]
    sub = w[np.ix_(ids, ids)] if ids else np.zeros((0, 0), dtype=np.int64)
    exact = len(ids) <= EXACT_LIMIT
    local = _exact_order(sub) if exact else _greedy_order(sub)
    return [ids[i] for i in local] + ([core] if 0 <= core < n else []), exact


def forward_weight(weights, order: list[int]) -> tuple[int, int]:
    """(weight whose provider precedes its consumer in `order`, total cross-pillar weight)."""
    w = np.asarray(weights, dtype=np.int64)
    pos = {p: i for i, p in enumerate(order)}
    forward = total = 0
    for u in order:
        for d in order:
            if u == d or not w[u, d]:
                continue
            total += int(w[u, d])
            if pos[d] < pos[u]:
                forward += int(w[u, d])
    return forward, total


def pillar_flow(weights, dom_share: dict[int, float] | None = None, core: int = 0) -> Flow:
    """Tier every pillar and order the chapters as four contiguous acts.

    foundations: a net provider, (in - out) / (in + out) >= FOUNDATION_NET;
    bottleneck:  a net consumer holding one theorem that dominates >= BOTTLENECK_DOM of the live cone;
    machinery:   everything between;  main: the core.
    Inside an act the pillars keep their position in the exact minimum-feedback order.
    """
    w = np.array(weights, dtype=np.int64)
    np.fill_diagonal(w, 0)
    n = len(w)
    dom_share = dom_share or {}
    base, exact = flow_order(w, core)
    ref_out = {p: int(w[p].sum()) for p in range(n)}
    ref_in = {p: int(w[:, p].sum()) for p in range(n)}

    def tier(p: int) -> str:
        if p == core:
            return "main"
        both = ref_in[p] + ref_out[p]
        net = (ref_in[p] - ref_out[p]) / both if both else 0.0
        if net >= FOUNDATION_NET:
            return "foundations"
        if net < 0 and dom_share.get(p, 0.0) >= BOTTLENECK_DOM:
            return "bottleneck"
        return "machinery"

    tiers = {p: tier(p) for p in range(n)}
    position = {p: i for i, p in enumerate(base)}
    order = sorted(base, key=lambda p: (TIERS.index(tiers[p]), position[p]))
    forward, total = forward_weight(w, order)
    return Flow(order=order, rank={p: i for i, p in enumerate(order)}, tier=tiers, ref_in=ref_in, ref_out=ref_out,
                forward=forward, total=total, exact=exact)


def net_flows(weights, order: list[int] | None = None) -> list[dict[str, int]]:
    """Directed pairs provider -> consumer with their net reference weight, heaviest first."""
    w = np.asarray(weights, dtype=np.int64)
    n = len(w)
    pos = {p: i for i, p in enumerate(order)} if order else {p: p for p in range(n)}
    out = []
    for a in range(n):
        for b in range(a + 1, n):
            ab, ba = int(w[a, b]), int(w[b, a])  # ab: a builds on b
            if ab == ba:
                continue
            provider, consumer = (b, a) if ab > ba else (a, b)
            out.append({"provider": provider, "consumer": consumer, "net": abs(ab - ba),
                        "forward": max(ab, ba), "backward": min(ab, ba)})
    out.sort(key=lambda f: (-f["net"], pos.get(f["provider"], 0), pos.get(f["consumer"], 0)))
    return out
