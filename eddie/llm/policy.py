"""Region policy and the strict wire schema for `/v1/responses` bodies.

The same schema is enforced twice: by the gateway before reserving, and by
`GovernedTransport` on the exact bytes about to be sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from eddie.config import EMBER_MODEL, MIN_MAX_OUTPUT_TOKENS
from eddie.llm.errors import ForbiddenParameterError, MissingParameterError
from eddie.llm.types import EFFORTS, EddieRequest


@dataclass(frozen=True)
class RegionPolicy:
    efforts: frozenset[str]
    max_output_ceiling: int
    required_summary: str | None = None


# Widened on purpose so Night 2 can escalate effort progressively.
# `none` and implicit `max` are impossible everywhere.
REGION_POLICY: Mapping[str, RegionPolicy] = {
    # 4,096 so one structured triage batch can carry 15 to 25 declarations (Day 3)
    "R1.triage": RegionPolicy(frozenset({"minimal", "low"}), 4_096),
    "R1.worker": RegionPolicy(frozenset({"low", "medium", "high"}), 16_384),
    "R2.critic": RegionPolicy(frozenset({"low", "medium", "high"}), 4_096),
    "R3.arbiter": RegionPolicy(frozenset({"high", "xhigh", "max"}), 32_768, required_summary="concise"),
    "R4.consolidator": RegionPolicy(frozenset({"medium", "high"}), 32_768),
}

ALLOWED_MODELS = frozenset({EMBER_MODEL})
REQUIRED_TOP_LEVEL = frozenset({"model", "input", "reasoning", "max_output_tokens", "store"})
ALLOWED_TOP_LEVEL = REQUIRED_TOP_LEVEL | frozenset(
    {"instructions", "include", "prompt_cache_key", "temperature", "text"}
)
ALLOWED_REASONING_KEYS = frozenset({"effort", "summary"})
ALLOWED_SUMMARIES = frozenset({"auto", "concise", "detailed"})
ALLOWED_INCLUDE = frozenset({"reasoning.encrypted_content"})
ALLOWED_ROLES = frozenset({"system", "developer", "user", "assistant"})
ALLOWED_PART_TYPES = frozenset({"input_text", "input_image", "input_file", "input_audio"})


def check_region_policy(region: str, effort: str, max_output_tokens: int, summary: str | None) -> None:
    policy = REGION_POLICY.get(region)
    if policy is None:
        raise ForbiddenParameterError(f"unknown brain_region {region!r}")
    if effort not in EFFORTS:
        raise ForbiddenParameterError(f"effort {effort!r} is not one of {EFFORTS}")
    if effort not in policy.efforts:
        raise ForbiddenParameterError(
            f"effort {effort!r} is not allowed for {region} (allowed: {sorted(policy.efforts)})"
        )
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int):
        raise MissingParameterError("max_output_tokens must be an explicit integer")
    if max_output_tokens < MIN_MAX_OUTPUT_TOKENS:
        raise ForbiddenParameterError(f"max_output_tokens must be >= {MIN_MAX_OUTPUT_TOKENS}")
    if max_output_tokens > policy.max_output_ceiling:
        raise ForbiddenParameterError(
            f"max_output_tokens {max_output_tokens} exceeds the {region} ceiling {policy.max_output_ceiling}"
        )
    if policy.required_summary and summary != policy.required_summary:
        raise ForbiddenParameterError(f"{region} requires reasoning.summary={policy.required_summary!r}")


def check_request(req: EddieRequest) -> None:
    check_region_policy(req.brain_region, req.effort, req.max_output_tokens, req.reasoning_summary)


def validate_wire_body(body: Any) -> None:
    """Strict schema for a `/v1/responses` body. Raises on anything unexpected."""
    if not isinstance(body, dict):
        raise ForbiddenParameterError("body must be a JSON object")
    keys = set(body)
    missing = REQUIRED_TOP_LEVEL - keys
    if missing:
        raise MissingParameterError(f"body is missing mandatory keys: {sorted(missing)}")
    extra = keys - ALLOWED_TOP_LEVEL
    if extra:
        raise ForbiddenParameterError(f"body carries forbidden keys: {sorted(extra)}")
    if body["model"] not in ALLOWED_MODELS:
        raise ForbiddenParameterError(f"model {body['model']!r} is not allowed")
    if body["store"] is not False:
        raise ForbiddenParameterError("store must be explicitly false")

    reasoning = body["reasoning"]
    if not isinstance(reasoning, dict) or "effort" not in reasoning:
        raise MissingParameterError("reasoning.effort is mandatory")
    if set(reasoning) - ALLOWED_REASONING_KEYS:
        raise ForbiddenParameterError(f"reasoning carries forbidden keys: {sorted(set(reasoning) - ALLOWED_REASONING_KEYS)}")
    if reasoning["effort"] not in EFFORTS:
        raise ForbiddenParameterError(f"reasoning.effort {reasoning['effort']!r} is not allowed")
    if "summary" in reasoning and reasoning["summary"] not in ALLOWED_SUMMARIES:
        raise ForbiddenParameterError(f"reasoning.summary {reasoning['summary']!r} is not allowed")

    m = body["max_output_tokens"]
    if isinstance(m, bool) or not isinstance(m, int) or m < MIN_MAX_OUTPUT_TOKENS:
        raise ForbiddenParameterError(f"max_output_tokens must be an integer >= {MIN_MAX_OUTPUT_TOKENS}")

    include = body.get("include", [])
    if not isinstance(include, list) or set(include) - ALLOWED_INCLUDE:
        raise ForbiddenParameterError(f"include may only contain {sorted(ALLOWED_INCLUDE)}")
    if "temperature" in body:
        t = body["temperature"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 2:
            raise ForbiddenParameterError("temperature must be a number within 0..2")
    if "text" in body and not isinstance(body["text"], dict):
        raise ForbiddenParameterError("text must be an object")
    if "instructions" in body and not isinstance(body["instructions"], str):
        raise ForbiddenParameterError("instructions must be a string")
    if "prompt_cache_key" in body and not isinstance(body["prompt_cache_key"], str):
        raise ForbiddenParameterError("prompt_cache_key must be a string")

    messages = body["input"]
    if not isinstance(messages, list) or not messages:
        raise ForbiddenParameterError("input must be a non-empty list of messages")
    for msg in messages:
        if not isinstance(msg, dict) or set(msg) != {"role", "content"}:
            raise ForbiddenParameterError("each input item must be exactly {role, content}")
        if msg["role"] not in ALLOWED_ROLES:
            raise ForbiddenParameterError(f"role {msg['role']!r} is not allowed")
        if not isinstance(msg["content"], list) or not msg["content"]:
            raise ForbiddenParameterError("message content must be a non-empty list")
        for part in msg["content"]:
            if not isinstance(part, dict) or part.get("type") not in ALLOWED_PART_TYPES:
                raise ForbiddenParameterError(f"content part type {part.get('type') if isinstance(part, dict) else part!r} is not allowed")
            if "file_id" in part:
                raise ForbiddenParameterError("file_id inputs are not allowed (unbounded, server-side)")
