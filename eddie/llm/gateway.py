"""EddieLLMGateway: the single pipeline every model call goes through.

validate -> canonical body -> upper-bound estimate -> hard limits -> reserve -> dispatch
-> send (adapter) -> settle

`EDDIE_BACKEND` selects mock (default, $0), dgx ($0, local) or ember (paid). Every backend
runs the identical validation and estimation, so mock runs surface exactly the errors a
live run would. Only ember touches the billable ledger.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

import httpx

from eddie.config import (
    CONTEXT_WINDOW_TOKENS,
    HARD_INPUT_TOKEN_LIMIT,
    HTTP_TOTAL_TIMEOUT_S,
    Settings,
    load_settings,
)
from eddie.llm.adapters.base import AdapterResult, output_text
from eddie.llm.adapters.dgx import DgxAdapter
from eddie.llm.adapters.mock import MockAdapter
from eddie.llm.body import build_body, canonical_bytes, sha256_hex
from eddie.llm.errors import (
    ForbiddenParameterError,
    HardInputTokenLimitError,
    TransportGuardError,
    UnboundedInputError,
    UpstreamError,
)
from eddie.llm.estimate import Estimate, estimate_body
from eddie.llm.governor import Reservation, Settlement, SpendGovernor, check_live_flags
from eddie.llm.policy import check_request, validate_wire_body
from eddie.llm.types import EddieRequest, EddieResponse, Usage

MAX_ATTEMPTS = 3  # only 429s are retried, each on a fresh reservation
MAX_BACKOFF_S = 60.0


class EddieLLMGateway:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        ember_transport: httpx.AsyncBaseTransport | None = None,
        dgx_transport: httpx.AsyncBaseTransport | None = None,
        mock_faults: list[str] | None = None,
        mock_latency_s: float = 0.0,
        sleep=asyncio.sleep,
    ) -> None:
        self.settings = settings or load_settings()
        self.backend = self.settings.backend
        self._sleep = sleep
        self._sem = asyncio.Semaphore(self.settings.max_concurrency)
        if self.backend == "ember":
            from eddie.llm.adapters.ember import EmberAdapter
            from eddie.llm.transport import KeyProvider

            real_network = ember_transport is None
            check_live_flags(self.settings, real_network=real_network)  # before any file is touched
            self.governor = SpendGovernor(self.settings, billable=True)  # refuses if not initialized
            self.governor.assert_live_allowed(real_network=real_network)
            keys = KeyProvider(self.settings.key_file)  # pops META_API_KEY out of os.environ
            keys.check()
            self.adapter: Any = EmberAdapter(self.governor, keys, inner=ember_transport)
        else:
            os.environ.pop("META_API_KEY", None)  # never leave a key where subprocesses inherit it
            self.governor = SpendGovernor(self.settings, billable=False)
            if self.backend == "dgx":
                self.adapter = DgxAdapter(self.settings, transport=dgx_transport)
            else:
                self.adapter = MockAdapter(faults=mock_faults, latency_s=mock_latency_s)

    async def aclose(self) -> None:
        await self.adapter.aclose()

    async def __aenter__(self) -> EddieLLMGateway:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- pre-flight (pure, before any reservation) ------------------------------------------

    def preflight(self, req: EddieRequest | dict[str, Any]) -> tuple[EddieRequest, dict, bytes, str, Estimate]:
        if isinstance(req, dict):
            req = EddieRequest.create(**req)
        check_request(req)
        body = build_body(req)
        validate_wire_body(body)
        self.adapter.check_body(body)
        data = canonical_bytes(body)
        estimate = estimate_body(body)
        if estimate.input_tokens > HARD_INPUT_TOKEN_LIMIT:
            raise HardInputTokenLimitError(estimate.input_tokens, HARD_INPUT_TOKEN_LIMIT)
        if not estimate.bounded and not req.allow_unbounded:
            raise UnboundedInputError(
                "request carries parts that cannot be bounded locally: " + "; ".join(estimate.unbounded)
            )
        if estimate.input_tokens + req.max_output_tokens > CONTEXT_WINDOW_TOKENS:
            raise ForbiddenParameterError("input + max_output_tokens exceeds the context window")
        return req, body, data, sha256_hex(data), estimate

    # --- the call ---------------------------------------------------------------------------

    async def respond(self, req: EddieRequest | dict[str, Any]) -> EddieResponse:
        req, body, data, digest, estimate = self.preflight(req)
        async with self._sem:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                res = await asyncio.to_thread(
                    self.governor.reserve,
                    backend=self.backend,
                    region=req.brain_region,
                    effort=req.effort,
                    max_output=req.max_output_tokens,
                    estimate=estimate,
                    body_sha256=digest,
                    allow_unbounded=req.allow_unbounded,
                    tag=req.tag,
                )
                await asyncio.to_thread(self.governor.dispatch, res)
                started = time.monotonic()
                result = await self._send(data, body, res)
                latency_ms = int((time.monotonic() - started) * 1000)

                if 200 <= result.status < 300:
                    settlement = await asyncio.to_thread(
                        self.governor.settle_response,
                        res,
                        result.payload,
                        http_status=result.status,
                        latency_ms=latency_ms,
                    )
                    return self._response(req, res, result, settlement, latency_ms)

                if result.status == 429:
                    await asyncio.to_thread(
                        self.governor.settle_failure, res, http_status=429, error="rate limited", kind="rate_limited"
                    )
                    if attempt < MAX_ATTEMPTS:
                        await self._sleep(self._backoff(attempt, result.retry_after))
                        continue
                    raise UpstreamError("rate limited (429) after bounded retries", status=429, retry_after=result.retry_after)

                kind = {401: "auth", 403: "auth", 402: "billing"}.get(result.status, "ambiguous")
                await asyncio.to_thread(
                    self.governor.settle_failure,
                    res,
                    http_status=result.status,
                    error=_error_text(result.payload, result.status),
                    kind=kind,
                )
                raise UpstreamError(
                    f"{self.backend} returned HTTP {result.status}: {_error_text(result.payload, result.status)}",
                    status=result.status,
                )
        raise AssertionError("unreachable")

    async def _send(self, data: bytes, body: dict, res: Reservation) -> AdapterResult:
        try:
            async with asyncio.timeout(HTTP_TOTAL_TIMEOUT_S):
                return await self.adapter.send(data, body, res)
        except TransportGuardError as exc:
            await asyncio.to_thread(
                self.governor.settle_failure, res, http_status=None, error=f"transport refused: {exc}", kind="never_sent"
            )
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection never opened, so the request never reached the server.
            await asyncio.to_thread(
                self.governor.settle_failure, res, http_status=None, error=f"connect failed: {exc}", kind="never_sent"
            )
            raise UpstreamError(f"could not connect to {self.backend}: {exc}") from exc
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            # The request may have been processed and billed: charge the full reservation.
            await asyncio.to_thread(
                self.governor.settle_failure,
                res,
                http_status=None,
                error=f"{type(exc).__name__}: {exc}",
                kind="ambiguous",
            )
            raise UpstreamError(f"{self.backend} call failed after sending: {type(exc).__name__}") from exc
        except Exception as exc:
            # Unknown failure after dispatch: assume it may have been billed.
            await asyncio.to_thread(
                self.governor.settle_failure,
                res,
                http_status=None,
                error=f"unexpected {type(exc).__name__}: {exc}",
                kind="ambiguous",
            )
            raise

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(MAX_BACKOFF_S, retry_after) + random.uniform(0, 0.5)
        return random.uniform(0, min(MAX_BACKOFF_S, 2.0 ** (attempt - 1)))

    def _response(
        self, req: EddieRequest, res: Reservation, result: AdapterResult, settlement: Settlement, latency_ms: int
    ) -> EddieResponse:
        text, summaries = output_text(result.payload)
        check = settlement.check
        usage = check.usage if check is not None and check.usage is not None else Usage(0, 0, 0, 0)
        warnings = list(check.warnings) if check is not None else []
        if check is not None and check.parse_error:
            warnings.append(f"usage unusable ({check.parse_error}); charged the full reservation")
        if settlement.tripped:
            warnings.append(f"BREAKER TRIPPED: {settlement.tripped}")
        payload = result.payload or {}
        return EddieResponse(
            call_id=res.call_id,
            backend=self.backend,
            model=str(payload.get("model") or ""),
            brain_region=req.brain_region,
            effort=req.effort,
            status=str(payload.get("status") or ""),
            text=text,
            usage=usage,
            cost_nano=settlement.charged_nano,
            shadow_nano=settlement.shadow_nano,
            latency_ms=latency_ms,
            response_id=payload.get("id"),
            reasoning_summary=summaries,
            warnings=warnings,
            raw=result.payload,
        )


def _error_text(payload: dict | None, status: int) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        return str(err.get("message") or err.get("code") or err.get("type") or status)[:300]
    return f"HTTP {status}"
