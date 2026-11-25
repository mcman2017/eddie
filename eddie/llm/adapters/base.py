"""Adapter protocol shared by the mock, dgx and ember backends."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from eddie.llm.governor import Reservation


@dataclass
class AdapterResult:
    status: int
    payload: dict[str, Any] | None  # normalized to the Responses-API shape (Meta usage names)
    headers: Mapping[str, str] = field(default_factory=dict)
    retry_after: float | None = None


class Adapter(Protocol):
    name: str
    billable: bool

    def check_body(self, body: dict[str, Any]) -> None:
        """Raise before reservation if this backend cannot carry the body."""

    async def send(self, data: bytes, body: dict[str, Any], res: Reservation) -> AdapterResult: ...

    async def aclose(self) -> None: ...


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def output_text(payload: dict[str, Any] | None) -> tuple[str, list[str]]:
    """Visible text and reasoning-summary texts from a Responses-API payload."""
    if not isinstance(payload, dict):
        return "", []
    texts: list[str] = []
    summaries: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    texts.append(str(part.get("text", "")))
        elif item.get("type") == "reasoning":
            for summary in item.get("summary") or []:
                if isinstance(summary, dict) and summary.get("text"):
                    summaries.append(str(summary["text"]))
    return "".join(texts), summaries
