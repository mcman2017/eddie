"""Request / response types.

`EddieRequest` has NO defaults for the three parameters that decide cost and accounting:
`brain_region`, `effort` and `max_output_tokens`. Ember silently defaults to
`effort="max"` when the field is omitted; Eddie makes omission impossible instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eddie.llm.errors import ForbiddenParameterError, MissingParameterError

Effort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
Region = Literal["R1.triage", "R1.worker", "R2.critic", "R3.arbiter", "R4.consolidator"]
ReasoningSummary = Literal["auto", "concise", "detailed"]
Role = Literal["system", "developer", "user", "assistant"]

EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")
REGIONS: tuple[str, ...] = ("R1.triage", "R1.worker", "R2.critic", "R3.arbiter", "R4.consolidator")


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextPart(_Part):
    type: Literal["text"] = "text"
    text: str


class ImagePart(_Part):
    """Inline image bytes (PNG or JPEG). Sent as a base64 data URL."""

    type: Literal["image"] = "image"
    data: bytes
    mime: Literal["image/png", "image/jpeg"]


class ImageURLPart(_Part):
    """Remote image; the server fetches it, so its size cannot be bounded locally."""

    type: Literal["image_url"] = "image_url"
    url: str


class AudioPart(_Part):
    type: Literal["audio"] = "audio"
    data: bytes
    format: Literal["wav", "mp3"]


class PdfPart(_Part):
    type: Literal["pdf"] = "pdf"
    data: bytes
    filename: str = "document.pdf"


Part = Annotated[TextPart | ImagePart | ImageURLPart | AudioPart | PdfPart, Field(discriminator="type")]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Role
    content: list[Part] = Field(min_length=1)


class EddieRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- mandatory: no defaults, ever ---
    brain_region: Region
    effort: Effort
    max_output_tokens: int
    messages: list[Message] = Field(min_length=1)

    # --- optional ---
    instructions: str | None = None
    reasoning_summary: ReasoningSummary | None = None
    include_encrypted_reasoning: bool = False
    prompt_cache_key: str | None = Field(default=None, max_length=256)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    text_format: dict[str, Any] | None = None  # -> Responses `text` (structured output)
    allow_unbounded: bool = False  # opt in to a worst-case W(m) reservation
    tag: str | None = Field(default=None, max_length=120)  # free-form telemetry label

    @classmethod
    def create(cls, **data: Any) -> EddieRequest:
        """Build a request, mapping validation failures onto Eddie's guard errors."""
        if data.get("effort") == "none":
            raise ForbiddenParameterError(
                "effort='none' is not allowed: Ember requires minimal|low|medium|high|xhigh|max"
            )
        try:
            return cls(**data)
        except ValidationError as exc:
            missing = [".".join(map(str, e["loc"])) for e in exc.errors() if e["type"] == "missing"]
            if missing:
                raise MissingParameterError(f"missing mandatory parameter(s): {', '.join(missing)}") from exc
            raise ForbiddenParameterError(str(exc)) from exc

    @classmethod
    def simple(cls, prompt: str, **data: Any) -> EddieRequest:
        """Convenience: one user message with one text part."""
        return cls.create(messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}], **data)


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EddieResponse:
    call_id: str
    backend: str
    model: str
    brain_region: str
    effort: str
    status: str
    text: str
    usage: Usage
    cost_nano: int  # billable actual (0 for mock/dgx)
    shadow_nano: int  # what Ember would have charged for the same usage
    latency_ms: int
    response_id: str | None = None
    reasoning_summary: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None
