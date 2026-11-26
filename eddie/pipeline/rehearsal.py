"""Canned, schema-valid replies for a $0 rehearsal on the mock backend.

Every text says `[mock]`, and the rows land in the rehearsal database with source='mock', so
nothing here can be mistaken for Ember output. The worker returns a real (tiny) plotting script,
so the sandbox, the QC and the critic loop all run for real. The critic rejects the calibration
draft once, so the rejected-then-repaired path is exercised too.
"""

from __future__ import annotations

import json
import re
from typing import Any

from eddie.pipeline.prompts import SECTIONS

SCRIPT = '''import numpy as np
import matplotlib.pyplot as plt

x = np.linspace(0.0, 2 * np.pi, 400)
y = np.sin(3 * x) * np.exp(-x / 4)
fig, ax = plt.subplots()
ax.plot(x, y, label="damped wave")
ax.axhline(0.0, linewidth=0.8)
ax.set_title("[mock] rehearsal witness")
ax.set_xlabel("x (radians)")
ax.set_ylabel("amplitude")
ax.legend(loc="upper right")
fig.tight_layout()
fig.savefig("witness.png")
print("rehearsal: peak", round(float(y.max()), 4))
'''


def _schema_name(body: dict[str, Any]) -> str:
    fmt = (body.get("text") or {}).get("format") or {}
    name = str(fmt.get("name") or "")
    if name.startswith("eddie_"):
        return name[len("eddie_"):]
    instructions = body.get("instructions") or ""
    for key, schema in (("python_source", "worker"), ("legibility", "critic"), ("intuition_md", "arbiter"),
                        ("synthesis_md", "chapter"), ('"items"', "triage")):
        if key in instructions:
            return schema
    return "triage"


def _user_text(body: dict[str, Any]) -> str:
    out = []
    for msg in body.get("input", []):
        for part in msg.get("content", []):
            if part.get("type") == "input_text":
                out.append(part.get("text", ""))
    return "\n".join(out)


def _filler(words: int) -> str:
    sentence = ("[mock rehearsal] This paragraph stands in for the chapter text that the R4 consolidator writes on "
                "a live run, so the layout, the length checks and the reader can be tested at no cost.")
    n = len(sentence.split())
    return " ".join([sentence] * max(1, words // n))


def respond(body: dict[str, Any]) -> str:
    schema = _schema_name(body)
    text = _user_text(body)
    draft = "Write a short Python script" in (body.get("instructions") or "")
    if schema == "triage":
        ids = list(dict.fromkeys(re.findall(r"\[(d\d+)\]", text))) or ["d1"]
        return json.dumps({"items": [{
            "id": i, "plain_english": f"[mock] a plain-English reading of declaration {i}.", "role": "key_lemma",
            "area": "mock area", "visualizability": 3 if n == 0 else 1, "visual_hint": "[mock] a damped wave",
        } for n, i in enumerate(ids)]})
    if schema == "worker":
        return json.dumps({"title": "[mock] rehearsal draft" if draft else "[mock] rehearsal witness",
                           "caption": "[mock] A damped sine wave computed with numpy, standing in for a real witness.",
                           "relation": "analogy", "python_source": SCRIPT})
    if schema == "critic":
        if "rehearsal draft" in text:
            return json.dumps({"verdict": "rejected", "legibility": 1, "faithfulness": 1,
                               "defects": ["[mock] the draft was written without the plot checklist"],
                               "fixes": ["[mock] follow the checklist"], "summary": "[mock] rejected draft"})
        return json.dumps({"verdict": "verified", "legibility": 3, "faithfulness": 3, "defects": [], "fixes": [],
                           "summary": "[mock] a clear, labelled plot"})
    if schema == "arbiter":
        m = re.search(r"\[target\] \w+ `([^`]+)`", text)
        name = m.group(1) if m else "target"
        return json.dumps({"verdict": "faithful", "intuition_md": f"[mock] `{name}` says what its statement says.",
                           "hypotheses": [{"name": "implicit", "plain_english": "[mock] none beyond the types"}],
                           "citations": [name], "caveats": ["[mock] rehearsal output"], "confidence": "low"})
    if schema == "chapter":
        md = "\n\n".join(f"### {title}\n\n{_filler(190)}" for title in SECTIONS)
        return json.dumps({"headline": "[mock] a rehearsal chapter", "synthesis_md": md,
                           "key_terms": [{"term": "mock", "plain": "rehearsal text, not a model's"}]})
    return json.dumps({})
