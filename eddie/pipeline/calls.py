"""The one place the pipeline talks to a model: `Caller.call`.

Each call is tagged with its step (`d3:<phase>:<step-hash>:a<attempt>`), so the ledger can say
which step every cent went to, and a resumed run can tell a settled step from a lost one.
The 2xx payload is journaled to disk before the gateway settles it. After the call, a display
copy of its receipt goes into `ember_calls`. Parsing happens last: a reply that cannot be used
is still a paid, recorded call.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eddie.llm.gateway import EddieLLMGateway
from eddie.llm.types import EddieRequest, EddieResponse
from eddie.pipeline import prompts
from eddie.pipeline.schemas import MODELS, parse_json, validate_text_format

TAG_PREFIX = "d3"


def step_hash(step_key: str) -> str:
    return hashlib.sha256(step_key.encode()).hexdigest()[:12]


def make_tag(phase: str, step_key: str, attempt: int) -> str:
    return f"{TAG_PREFIX}:{phase}:{step_hash(step_key)}:a{attempt}"


def phase_tag_like(phase: str) -> str:
    return f"{TAG_PREFIX}:{phase}:%"


@dataclass
class CallResult:
    status: str  # completed | incomplete | invalid
    data: Any = None
    text: str = ""
    error: str | None = None
    response: EddieResponse | None = None
    tag: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.data is not None

    @property
    def call_id(self) -> str | None:
        return self.response.call_id if self.response else None

    @property
    def response_id(self) -> str | None:
        return self.response.response_id if self.response else None

    @property
    def cost_nano(self) -> int:
        return self.response.cost_nano if self.response else 0


class Journal:
    """Raw 2xx payloads on disk, one file per ledger call id (git-ignored, under data/state/)."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, call_id: str) -> Path:
        return self.dir / f"{call_id}.json"

    def write(self, call_id: str, tag: str, payload: dict[str, Any] | None) -> None:
        target = self.path(call_id)
        tmp = self.dir / f".{call_id}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w") as fh:
            json.dump({"call_id": call_id, "tag": tag, "payload": payload}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    def read(self, call_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.path(call_id).read_text())
        except (FileNotFoundError, ValueError):
            return None


class Caller:
    def __init__(
        self,
        gateway: EddieLLMGateway,
        *,
        journal: Journal,
        phase: str,
        structured: bool,
        record: Callable[..., Any],
    ) -> None:
        self.gateway = gateway
        self.journal = journal
        self.phase = phase
        self.structured = structured
        self._record = record  # writes an ember_calls row (through the single store writer)

    def build(self, **kw: Any) -> EddieRequest:
        return build_request(phase=self.phase, structured=self.structured, **kw)

    async def call(self, req: EddieRequest, *, step_key: str, schema: str) -> CallResult:
        """Send one request. Guard and upstream errors propagate for the orchestrator to classify."""
        tag = req.tag or ""

        def journal(call_id: str, payload: dict[str, Any] | None) -> None:
            self.journal.write(call_id, tag, payload)

        resp = await self.gateway.respond(req, journal=journal)
        u = resp.usage
        await self._record(
            call_id=resp.call_id, backend=resp.backend, region=resp.brain_region, effort=resp.effort, step_key=step_key,
            tag=tag, response_id=resp.response_id, status=resp.status, input_tokens=u.input_tokens,
            cached_tokens=u.cached_tokens, output_tokens=u.output_tokens, reasoning_tokens=u.reasoning_tokens,
            cost_nano=resp.cost_nano, shadow_nano=resp.shadow_nano, latency_ms=resp.latency_ms,
            prompt_version=prompts.PROMPT_VERSION,
        )
        return interpret(resp, schema=schema, tag=tag)


def build_request(
    *,
    phase: str,
    structured: bool,
    step_key: str,
    attempt: int,
    region: str,
    effort: str,
    max_output: int,
    instructions: str,
    parts: list[dict[str, Any]],
    schema: str,
    summary: str | None = None,
) -> EddieRequest:
    """The exact request a slot sends (also used by the dry-run forecast)."""
    text_block = None
    if structured:
        text_block = prompts.text_format(schema)
        validate_text_format(text_block)
    else:
        instructions = instructions + prompts.prompt_json_suffix(schema)
    return EddieRequest.create(
        brain_region=region,
        effort=effort,
        max_output_tokens=max_output,
        messages=[{"role": "user", "content": parts}],
        instructions=instructions,
        reasoning_summary=summary,
        prompt_cache_key=prompts.cache_key(region, structured),
        text_format=text_block,
        tag=make_tag(phase, step_key, attempt),
    )


def interpret(resp: EddieResponse, *, schema: str, tag: str = "") -> CallResult:
    """Turn a settled response into data, or say why it cannot be used."""
    if resp.status and resp.status != "completed":
        return CallResult("incomplete", text=resp.text, error=f"status {resp.status}", response=resp, tag=tag,
                          warnings=list(resp.warnings))
    if not resp.text.strip():
        return CallResult("incomplete", error="no visible output", response=resp, tag=tag, warnings=list(resp.warnings))
    try:
        data = MODELS[schema].model_validate(parse_json(resp.text))
    except (ValueError, ValidationError) as exc:
        return CallResult("invalid", text=resp.text, error=f"{type(exc).__name__}: {str(exc)[:300]}", response=resp,
                          tag=tag, warnings=list(resp.warnings))
    return CallResult("completed", data=data, text=resp.text, response=resp, tag=tag, warnings=list(resp.warnings))


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_part(png: bytes) -> dict[str, Any]:
    return {"type": "image", "data": png, "mime": "image/png"}
