"""Deterministic, offline, $0 backend with Meta-exact usage field names and fault injection."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx

from eddie.llm.adapters.base import AdapterResult
from eddie.llm.governor import Reservation

_REASONING_PCT = {"minimal": 0, "low": 20, "medium": 40, "high": 60, "xhigh": 70, "max": 80}
FAULTS = ("429", "503", "500", "504", "401", "402", "timeout", "bad_usage", "no_usage")


class MockAdapter:
    name = "mock"
    billable = False

    def __init__(self, *, faults: list[str] | None = None, latency_s: float = 0.0) -> None:
        unknown = set(faults or []) - set(FAULTS)
        if unknown:
            raise ValueError(f"unknown mock faults: {sorted(unknown)}")
        self.faults = list(faults or [])
        self.latency_s = latency_s
        self.sends = 0

    def check_body(self, body: dict[str, Any]) -> None:
        return None

    async def send(self, data: bytes, body: dict[str, Any], res: Reservation) -> AdapterResult:
        self.sends += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        fault = self.faults.pop(0) if self.faults else None
        if fault == "timeout":
            raise httpx.ReadTimeout("mock read timeout")
        if fault in ("429", "503", "500", "504", "401", "402"):
            headers = {"retry-after": "0"} if fault in ("429", "503") else {}
            return AdapterResult(int(fault), {"error": {"type": "mock", "code": fault}}, headers, 0.0 if headers else None)

        digest = hashlib.sha256(data).hexdigest()
        pct = 40 + int(digest[0:2], 16) % 40
        input_tokens = max(1, min(res.est_input_bound, 1_000_000) * pct // 100)
        output_tokens = min(res.max_output, 32 + int(digest[2:4], 16) % 64)
        reasoning = output_tokens * _REASONING_PCT.get(res.effort, 0) // 100
        usage: Any = {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": input_tokens + output_tokens,
        }
        if fault == "bad_usage":
            usage = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}
        if fault == "no_usage":
            usage = None
        payload = {
            "id": f"resp_mock_{digest[:24]}",
            "object": "response",
            "model": body.get("model"),
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "id": f"rs_mock_{digest[:12]}",
                    "summary": [{"type": "summary_text", "text": f"mock reasoning at {res.effort}"}]
                    if body.get("reasoning", {}).get("summary")
                    else [],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"[mock {res.region}/{res.effort}] {digest[:16]}"}],
                },
            ],
            "usage": usage,
        }
        return AdapterResult(200, payload, {})

    async def aclose(self) -> None:
        return None
