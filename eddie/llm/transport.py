"""GovernedTransport: the only code that may send bytes to Ember or read its key.

Every request is re-validated at the wire, after the gateway's checks and immediately
before the socket opens:

* exact method, scheme, host, port and path;
* a single-use reservation id (from request extensions) in state `dispatched`, whose
  recorded body sha256 matches the bytes actually being sent;
* the strict body schema (store=false, explicit effort, no hidden-context keys);
* region policy, max_output_tokens and the upper-bound estimate against the reservation;
* the kill switch.

Only then is the `Authorization` header attached. Nothing else in Eddie ever holds the key.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import MutableMapping
from pathlib import Path

import httpx

from eddie.llm.body import sha256_hex
from eddie.llm.errors import GuardError, LiveEmberDisabledError, TransportGuardError
from eddie.llm.estimate import estimate_body
from eddie.llm.governor import running_under_pytest
from eddie.llm.ledger import Ledger
from eddie.llm.policy import check_region_policy, validate_wire_body

EMBER_HOST = "api.meta.ai"
EMBER_PATH = "/v1/responses"
EMBER_URL = f"https://{EMBER_HOST}{EMBER_PATH}"
RESERVATION_EXTENSION = "eddie_reservation"


class KeyProvider:
    """Keeps the Ember key out of `os.environ`; reads the key file only at send time."""

    def __init__(self, key_file: Path, *, env: MutableMapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self._key_file = Path(key_file)
        self._env_key = env.pop("META_API_KEY", None)  # removed so no subprocess inherits it

    def check(self) -> None:
        if self._env_key:
            return
        try:
            st = self._key_file.stat()
        except FileNotFoundError as exc:
            raise LiveEmberDisabledError(f"no Ember key file at {self._key_file}") from exc
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise LiveEmberDisabledError(f"{self._key_file} must be mode 600 (chmod 600 it)")

    def get(self) -> str:
        if self._env_key:
            return self._env_key
        self.check()
        key = self._key_file.read_text().strip()
        if not key:
            raise LiveEmberDisabledError(f"{self._key_file} is empty")
        return key

    def __repr__(self) -> str:  # never print the key
        return f"KeyProvider(file={self._key_file}, env_key={'set' if self._env_key else 'unset'})"


class GovernedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        ledger: Ledger,
        keys: KeyProvider,
        *,
        kill_switch: Path,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if inner is None:
            if running_under_pytest():
                raise LiveEmberDisabledError("the real network transport cannot be built under pytest")
            inner = httpx.AsyncHTTPTransport(retries=0)
        self._inner = inner
        self._ledger = ledger
        self._keys = keys
        self._kill_switch = kill_switch

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (
            request.method != "POST"
            or url.scheme != "https"
            or url.host != EMBER_HOST
            or url.port not in (None, 443)
            or url.path != EMBER_PATH
            or url.query
        ):
            raise TransportGuardError(f"refusing {request.method} {url}: only POST {EMBER_URL} is allowed")
        call_id = request.extensions.get(RESERVATION_EXTENSION)
        if not call_id:
            raise TransportGuardError("refusing an Ember request without a reservation")
        if self._kill_switch.exists():
            raise TransportGuardError("kill switch present: refusing to send")

        content = request.content
        try:
            body = json.loads(content)
        except ValueError as exc:
            raise TransportGuardError("request body is not JSON") from exc
        try:
            validate_wire_body(body)
            estimate = estimate_body(body)
        except GuardError as exc:
            raise TransportGuardError(f"body failed the wire schema: {exc}") from exc

        row = await asyncio.to_thread(self._ledger.claim_send, call_id, sha256_hex(content))
        effort = body["reasoning"]["effort"]
        if effort != row["effort"]:
            raise TransportGuardError("effort differs from the reservation")
        if body["max_output_tokens"] != row["max_output"]:
            raise TransportGuardError("max_output_tokens differs from the reservation")
        try:
            check_region_policy(row["region"], effort, row["max_output"], body["reasoning"].get("summary"))
        except GuardError as exc:
            raise TransportGuardError(f"region policy: {exc}") from exc
        if row["bounded"] and (not estimate.bounded or estimate.input_tokens > row["est_input"]):
            raise TransportGuardError("body estimate exceeds the reserved estimate")

        request.headers["Authorization"] = f"Bearer {self._keys.get()}"
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
