"""Canonical `/v1/responses` body.

One builder for every backend. The body is serialized exactly once; the governor
reserves against the sha256 of those bytes, and `GovernedTransport` refuses to send
any other bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from eddie.config import EMBER_MODEL
from eddie.llm.types import AudioPart, EddieRequest, ImagePart, ImageURLPart, PdfPart, TextPart


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _part(part: Any) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"type": "input_text", "text": part.text}
    if isinstance(part, ImagePart):
        return {"type": "input_image", "image_url": f"data:{part.mime};base64,{_b64(part.data)}"}
    if isinstance(part, ImageURLPart):
        return {"type": "input_image", "image_url": part.url}
    if isinstance(part, AudioPart):
        return {"type": "input_audio", "input_audio": {"data": _b64(part.data), "format": part.format}}
    if isinstance(part, PdfPart):
        return {
            "type": "input_file",
            "filename": part.filename,
            "file_data": f"data:application/pdf;base64,{_b64(part.data)}",
        }
    raise TypeError(f"unknown content part {type(part).__name__}")


def build_body(req: EddieRequest) -> dict[str, Any]:
    reasoning: dict[str, Any] = {"effort": req.effort}  # always explicit, never defaulted
    if req.reasoning_summary is not None:
        reasoning["summary"] = req.reasoning_summary
    body: dict[str, Any] = {
        "model": EMBER_MODEL,
        "input": [
            {"role": msg.role, "content": [_part(p) for p in msg.content]} for msg in req.messages
        ],
        "reasoning": reasoning,
        "max_output_tokens": req.max_output_tokens,
        "store": False,  # server default is true; stored context would be billed invisibly
    }
    if req.instructions is not None:
        body["instructions"] = req.instructions
    if req.include_encrypted_reasoning:
        body["include"] = ["reasoning.encrypted_content"]
    if req.prompt_cache_key:
        body["prompt_cache_key"] = req.prompt_cache_key
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.text_format is not None:
        body["text"] = req.text_format
    return body


def canonical_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
