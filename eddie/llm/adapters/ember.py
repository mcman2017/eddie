"""Paid backend: `muse-ember-1.0` on Meta's `/v1/responses`, always behind GovernedTransport."""

from __future__ import annotations

from typing import Any

import httpx

from eddie.config import HTTP_TOTAL_TIMEOUT_S
from eddie.llm.adapters.base import AdapterResult, parse_retry_after
from eddie.llm.governor import Reservation, SpendGovernor
from eddie.llm.transport import EMBER_URL, RESERVATION_EXTENSION, GovernedTransport, KeyProvider

RATE_LIMIT_HEADERS = (
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
)


class EmberAdapter:
    name = "ember"
    billable = True

    def __init__(
        self,
        governor: SpendGovernor,
        keys: KeyProvider,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        transport = GovernedTransport(
            governor.ledger, keys, kill_switch=governor.settings.kill_switch_path, inner=inner
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(connect=10.0, read=float(HTTP_TOTAL_TIMEOUT_S), write=60.0, pool=10.0),
            follow_redirects=False,
        )

    def check_body(self, body: dict[str, Any]) -> None:
        return None

    async def send(self, data: bytes, body: dict[str, Any], res: Reservation) -> AdapterResult:
        response = await self._client.post(
            EMBER_URL,
            content=data,
            headers={"Content-Type": "application/json"},
            extensions={RESERVATION_EXTENSION: res.call_id},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        headers = {k: response.headers[k] for k in RATE_LIMIT_HEADERS if k in response.headers}
        return AdapterResult(
            response.status_code,
            payload if isinstance(payload, dict) else None,
            headers,
            parse_retry_after(response.headers.get("retry-after")),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
