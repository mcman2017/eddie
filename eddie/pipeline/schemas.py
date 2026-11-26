"""Validation of what the regions send back, and of what we send.

* `validate_text_format` checks a structured-output block locally before any request leaves:
  a malformed schema would come back as HTTP 400, which the gateway (rightly, conservatively)
  charges as an ambiguous failure.
* `parse_json` accepts structured output or prompt-JSON (code fences and stray prose stripped).
* The pydantic models clip over-long text instead of failing the call: a paid answer that is a
  little long is still worth keeping. Missing or mismatched content fails.
* `prose` keeps model Markdown inside the subset `web/src/Prose.tsx` renders.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eddie.pipeline.prompts import RELATIONS, ROLES, SECTIONS

NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TYPES = {"object", "array", "string", "integer", "number", "boolean"}


class SchemaError(ValueError):
    pass


def _check_schema(node: Any, path: str) -> None:
    if not isinstance(node, dict) or node.get("type") not in _TYPES:
        raise SchemaError(f"{path}: every node needs a known type")
    if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
        raise SchemaError(f"{path}: enum must be a non-empty list")
    if node["type"] == "object":
        props = node.get("properties")
        if not isinstance(props, dict) or not props:
            raise SchemaError(f"{path}: an object needs properties")
        if node.get("additionalProperties") is not False:
            raise SchemaError(f"{path}: strict mode needs additionalProperties: false")
        if sorted(node.get("required", [])) != sorted(props):
            raise SchemaError(f"{path}: strict mode needs every property required")
        for key, child in props.items():
            _check_schema(child, f"{path}.{key}")
    elif node["type"] == "array":
        _check_schema(node.get("items"), f"{path}[]")


def validate_text_format(fmt: Any) -> None:
    """Refuse locally what the server would refuse: the `text` block of a /v1/responses body."""
    if not isinstance(fmt, dict) or set(fmt) != {"format"}:
        raise SchemaError("text must be exactly {format: ...}")
    f = fmt["format"]
    if not isinstance(f, dict) or f.get("type") != "json_schema" or f.get("strict") is not True:
        raise SchemaError("format must be a strict json_schema")
    if not isinstance(f.get("name"), str) or not NAME.fullmatch(f["name"]):
        raise SchemaError("format.name must match [A-Za-z0-9_-]{1,64}")
    if set(f) - {"type", "name", "schema", "strict", "description"}:
        raise SchemaError(f"format carries unknown keys {sorted(set(f) - {'type', 'name', 'schema', 'strict', 'description'})}")
    schema = f.get("schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise SchemaError("the root schema must be an object")
    _check_schema(schema, "$")


def parse_json(text: str) -> dict[str, Any]:
    """The first JSON object in `text` (fences and surrounding prose tolerated)."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\s*|\s*```$", "", s)
    try:
        value = json.loads(s)
    except ValueError:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the reply") from None
        value = json.loads(s[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("the reply is not a JSON object")
    return value


def words(text: str) -> int:
    return len(text.split())


def clip_words(text: str, n: int) -> str:
    parts = text.split()
    return text.strip() if len(parts) <= n else " ".join(parts[:n]) + " …"


def clip_chars(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TriageItem(_Model):
    id: str
    plain_english: str
    role: str
    area: str
    visualizability: int = Field(ge=0, le=3)
    visual_hint: str

    @field_validator("plain_english")
    @classmethod
    def _gloss(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty gloss")
        return clip_words(v, 60)

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        return v if v in ROLES else "structure"

    @field_validator("area")
    @classmethod
    def _area(cls, v: str) -> str:
        return clip_words(v, 6)

    @field_validator("visual_hint")
    @classmethod
    def _hint(cls, v: str) -> str:
        return clip_words(v, 30)


class TriageBatch(_Model):
    items: list[TriageItem]

    def by_id(self, expected: list[str]) -> dict[str, TriageItem]:
        """Items keyed by id; ids outside the batch are dropped, missing ones simply absent."""
        want = set(expected)
        out: dict[str, TriageItem] = {}
        for item in self.items:
            if item.id in want and item.id not in out:
                out[item.id] = item
        return out


class WorkerOutput(_Model):
    title: str
    caption: str
    relation: str
    python_source: str

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        return clip_chars(v, 72)

    @field_validator("caption")
    @classmethod
    def _caption(cls, v: str) -> str:
        return clip_words(v, 80)

    @field_validator("relation")
    @classmethod
    def _relation(cls, v: str) -> str:
        return v if v in RELATIONS else "numerical_illustration"

    @field_validator("python_source")
    @classmethod
    def _code(cls, v: str) -> str:
        v = v.strip()
        if v.startswith("```"):
            v = re.sub(r"^```[A-Za-z]*\s*|\s*```$", "", v)
        if not v.strip():
            raise ValueError("empty script")
        return v


class CriticOutput(_Model):
    verdict: str
    legibility: int = Field(ge=0, le=3)
    faithfulness: int = Field(ge=0, le=3)
    defects: list[str]
    fixes: list[str]
    summary: str

    @field_validator("verdict")
    @classmethod
    def _verdict(cls, v: str) -> str:
        if v not in ("verified", "rejected"):
            raise ValueError("verdict must be verified or rejected")
        return v

    @field_validator("defects", "fixes")
    @classmethod
    def _items(cls, v: list[str]) -> list[str]:
        return [clip_chars(x, 300) for x in v if x.strip()][:12]


class Hypothesis(_Model):
    name: str
    plain_english: str


class ArbiterOutput(_Model):
    verdict: str
    intuition_md: str
    hypotheses: list[Hypothesis]
    citations: list[str]
    caveats: list[str]
    confidence: str

    @field_validator("verdict")
    @classmethod
    def _verdict(cls, v: str) -> str:
        if v not in ("faithful", "needs_fix", "unfaithful"):
            raise ValueError("unknown verdict")
        return v

    @field_validator("intuition_md")
    @classmethod
    def _intuition(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty intuition")
        return clip_words(v, 240)

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, v: str) -> str:
        return v if v in ("low", "medium", "high") else "low"


class KeyTerm(_Model):
    term: str
    plain: str


class ChapterOutput(_Model):
    headline: str
    synthesis_md: str
    key_terms: list[KeyTerm]

    @field_validator("headline")
    @classmethod
    def _headline(cls, v: str) -> str:
        return clip_words(v, 16)


MODELS: dict[str, type[_Model]] = {
    "triage": TriageBatch, "worker": WorkerOutput, "critic": CriticOutput, "arbiter": ArbiterOutput,
    "chapter": ChapterOutput,
}

# --- the Markdown subset Prose.tsx renders ----------------------------------------------------

_LATEX = {
    r"\zeta": "ζ", r"\sigma": "σ", r"\chi": "χ", r"\rho": "ρ", r"\pi": "π", r"\infty": "∞", r"\leq": "≤",
    r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠", r"\in": "∈", r"\to": "→", r"\cdot": "·",
    r"\times": "×", r"\sum": "Σ", r"\prod": "Π", r"\mathbb{C}": "ℂ", r"\mathbb{R}": "ℝ", r"\mathbb{N}": "ℕ",
    r"\mathbb{Z}": "ℤ", r"\mathbb{Q}": "ℚ", r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\lambda": "λ",
}


def prose(md: str, allowed_names: set[str] | None = None) -> tuple[str, list[str]]:
    """Normalize model Markdown to Prose.tsx's subset. Returns (text, notes about what changed)."""
    notes: list[str] = []
    text = md.replace("\r", "")
    if re.search(r"<[a-zA-Z/][^>]*>", text):
        text = re.sub(r"<[a-zA-Z/][^>]*>", "", text)
        notes.append("html removed")
    if re.search(r"\[[^\]]+\]\([^)]+\)", text):
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        notes.append("links flattened")
    if "$" in text or "\\" in text:
        for k, v in _LATEX.items():
            text = text.replace(k, v)
        text = re.sub(r"\\frac\{([^}]*)\}\{([^}]*)\}", r"\1/\2", text)
        stripped = re.sub(r"\$+", "", text)
        if stripped != text:
            notes.append("latex flattened")
        text = stripped
    # *italic* and _italic_ (never **bold**) become plain text
    text = re.sub(r"(?<![*\w])\*(?!\*)([^*\n]+?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<![_\w])_(?!_)([^_\n]+?)_(?![_\w])", r"\1", text)
    # a heading is always its own block: Prose renders a heading only when it is alone in its block
    text = re.sub(r"\n?(#{1,6}) +(.+)\n?", lambda m: f"\n\n### {m.group(2).strip()}\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if allowed_names is not None:

        def keep(m: re.Match[str]) -> str:
            name = m.group(1)
            if name in allowed_names or not re.fullmatch(r"[A-Za-z_][\w'.₀-₉]*", name):
                return m.group(0)
            notes.append(f"unknown name {name}")
            return name

        text = re.sub(r"`([^`\n]+)`", keep, text)
    return text, notes


def chapter_problems(md: str) -> list[str]:
    """Hard failures for an R4 chapter (a retry is cheaper than shipping a broken chapter)."""
    problems = []
    headings = [h.strip() for h in re.findall(r"^### (.+)$", md, flags=re.M)]
    found = [h for h in headings if h in SECTIONS]
    if found != list(SECTIONS):
        problems.append(f"sections must be exactly {list(SECTIONS)} in order, got {headings}")
    n = words(md)
    if n < 400:
        problems.append(f"only {n} words (700 to 1,100 asked)")
    if n > 1_800:
        problems.append(f"{n} words (700 to 1,100 asked)")
    return problems
