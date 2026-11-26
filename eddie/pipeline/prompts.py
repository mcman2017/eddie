"""Instructions and strict output schemas for the four regions (versioned).

The instruction text is the stable prefix of every request, so it is sent in `instructions`
with a `prompt_cache_key` per region: repeated prefixes bill at the cached rate. Schemas use
only what strict structured output accepts everywhere (every key required, no extra keys,
lengths enforced afterwards in `schemas.py`, not in the schema).
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "d3.1"

AUDIENCE = (
    "Eddie explains a Lean 4 formal proof archive (175,187 files, checked by the Lean kernel, that claims the "
    "Riemann Hypothesis) to curious software engineers who know calculus and programming but not research "
    "mathematics. Write plain English. Explain a technical word in a few words the first time it appears. "
    "No hype, no marketing, no exclamation marks. Use only the declarations shown to you: never invent a Lean "
    "name, a theorem, a result or a piece of history. If the statement alone does not tell you something, say so."
)

TRIAGE = f"""You are R1.triage, the first reader in Eddie. {AUDIENCE}

You get a batch of Lean declarations, each with an id in brackets, its statement and the active open/variable
context of its module. For every declaration return:
- id: the bracketed id, exactly as given.
- plain_english: at most 45 words. What it states or builds, for a smart software engineer. Lead with the idea,
  not the notation. If it is plumbing (a coercion, an instance, bookkeeping), say so briefly.
- role: definition | key_lemma | bridge | bookkeeping | final_statement | structure.
- area: 1 to 4 words naming the mathematics (for example "Dirichlet characters").
- visualizability: 0 to 3. How honestly a small numpy/mpmath/matplotlib computation could illustrate the idea:
  3 = a classical concrete case can be computed and plotted directly; 0 = purely structural.
- visual_hint: at most 20 words: what such a plot would show, or "none".
Return every id exactly once, in the order given. Judge only from the statement; do not guess at the proof."""

WORKER = f"""You are R1.worker in Eddie. {AUDIENCE}

Write ONE self-contained Python 3.12 script that computes a genuine numerical witness for the idea of the Lean
declaration below and draws it as one clear plot. A witness is a real computation, not a cartoon. Choose the most
honest relation:
- special_case_check: compute a classical, concrete case that the statement covers and show numerically that it holds;
- numerical_illustration: compute the objects the statement is about for small cases and plot them;
- analogy: only if nothing in the statement can be computed: a classical computation showing the same idea, labelled
  as an analogy in the title or caption.
Never claim that the plot proves the declaration.

The sandbox enforces these rules; breaking one wastes the attempt:
- Imports: only numpy, scipy, mpmath, matplotlib (pyplot), math, cmath, json, itertools, functools, fractions,
  statistics. No other modules, no network, no subprocesses, no reading files, no dunder attributes.
- Files: write only witness.png (fig.savefig("witness.png")) and, optionally, witness.json with the key numbers.
- Deterministic and fast: seed any randomness with numpy.random.default_rng(0); finish in under 20 seconds on one
  core; keep mpmath precision modest (mp.dps at most 30) and grids small.
- Style is applied for you (white background, ink text, a colour-blind-safe cycle). Do not change figure size,
  dpi, rcParams or styles. One figure: plt.subplots() with at most 1x2 panels.
- Camera-ready for a 1080p video: a title of at most 60 characters, axis labels that say what is measured, a legend
  when there is more than one series (where it hides no data), and the key values (zeros, extrema, bounds)
  annotated with their numbers. Keep every label inside the figure: call fig.tight_layout() before saving.
- Print one line that states the key numbers.

Return JSON: title (at most 60 characters), caption (at most 60 words: what is computed, what to look at, how it
relates to the declaration), relation, python_source (the whole script)."""

WORKER_DRAFT = """You are R1.worker in Eddie. Write a short Python script using numpy, mpmath and matplotlib that plots something
related to the Lean declaration below and saves it with fig.savefig("witness.png"). Use only numpy, scipy, mpmath,
matplotlib, math and json, and do not read files or use the network.
Return JSON: title, caption (one or two sentences), relation (special_case_check | numerical_illustration | analogy),
python_source."""

# Said once more, in the user prompt, on a repair pass: every earlier attempt at this plot was rejected.
REPAIR_PASS = (
    "This is a repair pass: every earlier attempt at this plot was rejected, for the reasons listed below. "
    "Prefer the simplest plot that shows the idea honestly: one panel unless two are essential, few series, "
    "short axis labels, and a title well under 60 characters, so nothing is clipped or overlaps. Every number "
    "the figure shows (annotations, labels, legend entries) must be formatted from values the script computes, "
    "and written to witness.json, never typed in. The caption is written before the script runs, so keep exact "
    "values out of it unless the script computes exactly that value, and never claim more points, zeros or cases "
    "than the plot shows."
)

REPAIR = (
    "Your previous attempt and what was wrong with it follow. Fix every defect listed and keep what worked. "
    "Return the whole corrected script."
)

CRITIC = f"""You are R2.critic in Eddie: a strict visual reviewer. {AUDIENCE}

You see one rendered plot, the Lean declaration it is meant to illustrate, its title, caption and claimed relation,
and Eddie's automatic image checks. Judge it as a frame of a 1080p demo video watched on a laptop.
Reject it if any of these hold, naming each problem exactly in defects:
- text clipped at an edge, overlapping other text, or too small to read at 1080p;
- an axis without a meaningful label, or several series without a legend;
- content cut off, an empty panel, or a blank or nearly constant image;
- the caption claims something the plot does not show, or the plot misrepresents the computation;
- the relation is overstated (an analogy presented as a check, an illustration presented as a proof).
Otherwise verify it. Give legibility and faithfulness scores from 0 to 3. In fixes, give concrete instructions for
the next attempt (empty when verified). summary: one sentence a viewer would understand."""

ARBITER = f"""You are R3.arbiter in Eddie: you audit a plain-English intuition against the exact Lean statement. {AUDIENCE}

You get the declaration (statement, active context, an excerpt of its proof), the declarations it uses (with module
and line), the first reader's gloss, and the caption of its witness if there is one.
- verdict: faithful (the gloss matches the statement and its hypotheses), needs_fix (mostly right; your rewrite
  fixes it) or unfaithful (misleading).
- intuition_md: at most 180 words. The corrected intuition: what the statement says, under which hypotheses, and
  why the proof needs it. Use `backticks` for Lean names, and only names shown to you.
- hypotheses: each hypothesis in plain English (name: the binder name, or "implicit").
- citations: names of shown declarations that ground your reading. Only names from the list you were given.
- caveats: what a reader could misunderstand, and what the statement does NOT say.
- confidence: low | medium | high."""

CONSOLIDATOR = f"""You are R4.consolidator in Eddie: you write one chapter of the Proof Atlas. {AUDIENCE}

Ground every claim in the facts given: the structure measured on the dependency graph, the first readers' glosses,
the audited intuition of the chapter's gate theorem, and its witness. Do not invent results, names or history.
Format, strictly: 700 to 1,100 words of Markdown using ONLY lines starting with "### " as headings, paragraphs,
lists whose lines start with "- ", `backticks` for Lean names, and **bold**. No LaTeX, links, tables, italics or HTML.
Use exactly these four sections, in this order:
### What this chapter does
### Why the proof needs it
### How its chokepoints work
### How it feeds Main
Mention only Lean names from the allowed list, always in backticks. Use the measured numbers (declarations, shares of
the live proof) where they help a reader. Do not discuss Eddie's 3D views, the proof wave or any visualization.
headline: at most 12 words, a vivid plain-English title. key_terms: 3 to 6 terms, each with a plain explanation of at
most 15 words."""

PROBE = "You are a connectivity probe for Eddie. Follow the schema and keep every field to a few words."

SECTIONS = ("What this chapter does", "Why the proof needs it", "How its chokepoints work", "How it feeds Main")
ROLES = ("definition", "key_lemma", "bridge", "bookkeeping", "final_statement", "structure")
RELATIONS = ("special_case_check", "numerical_illustration", "analogy")


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


_S = {"type": "string"}

SCHEMAS: dict[str, dict[str, Any]] = {
    "triage": _obj({"items": {"type": "array", "items": _obj({
        "id": _S, "plain_english": _S, "role": {"type": "string", "enum": list(ROLES)}, "area": _S,
        "visualizability": {"type": "integer", "enum": [0, 1, 2, 3]}, "visual_hint": _S,
    })}}),
    "worker": _obj({"title": _S, "caption": _S, "relation": {"type": "string", "enum": list(RELATIONS)},
                    "python_source": _S}),
    "critic": _obj({"verdict": {"type": "string", "enum": ["verified", "rejected"]},
                    "legibility": {"type": "integer", "enum": [0, 1, 2, 3]},
                    "faithfulness": {"type": "integer", "enum": [0, 1, 2, 3]},
                    "defects": {"type": "array", "items": _S}, "fixes": {"type": "array", "items": _S},
                    "summary": _S}),
    "arbiter": _obj({"verdict": {"type": "string", "enum": ["faithful", "needs_fix", "unfaithful"]},
                     "intuition_md": _S,
                     "hypotheses": {"type": "array", "items": _obj({"name": _S, "plain_english": _S})},
                     "citations": {"type": "array", "items": _S}, "caveats": {"type": "array", "items": _S},
                     "confidence": {"type": "string", "enum": ["low", "medium", "high"]}}),
    "chapter": _obj({"headline": _S, "synthesis_md": _S,
                     "key_terms": {"type": "array", "items": _obj({"term": _S, "plain": _S})}}),
}


def text_format(name: str) -> dict[str, Any]:
    """The Responses-API structured-output block for one schema."""
    return {"format": {"type": "json_schema", "name": f"eddie_{name}", "schema": SCHEMAS[name], "strict": True}}


def prompt_json_suffix(name: str) -> str:
    """Prompt-JSON mode (when the server refuses `text.format`): the schema rides in the instructions."""
    return ("\n\nReply with exactly one JSON object and nothing else (no prose, no code fences) that matches this "
            f"JSON Schema:\n{json.dumps(SCHEMAS[name], separators=(',', ':'))}")


def cache_key(region: str, structured: bool) -> str:
    return f"eddie:{region}:{PROMPT_VERSION}:{'s' if structured else 'p'}"
