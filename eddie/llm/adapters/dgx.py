"""$0 local backend: an OpenAI-compatible `/v1/chat/completions` server on the DGX.

Translates the canonical Responses body to chat-completions, and normalizes the reply
back to the Responses shape with Meta's usage names, so the shadow ledger can price it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from eddie.config import ConfigError, Settings
from eddie.llm.adapters.base import AdapterResult, parse_retry_after
from eddie.llm.errors import UnsupportedModalityError
from eddie.llm.governor import Reservation

_EFFORT_MAP = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _refuse_paid_hosts(url: str) -> None:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        raise ConfigError(f"DGX base URL {url!r} has no host")
    if host == "meta.ai" or host.endswith(".meta.ai"):
        raise ConfigError("dgx backend may never point at a Meta host (that would be an unmetered paid call)")


def _read_token(path: Path | None) -> str | None:
    if path is None:
        return None
    token = path.read_text().strip()
    return token or None


class DgxAdapter:
    name = "dgx"
    billable = False

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.dgx_text_base_url:
            raise ConfigError("dgx backend needs DGX_TEXT_BASE_URL (e.g. the llm-gateway /v1 endpoint)")
        self.text_base = settings.dgx_text_base_url.rstrip("/")
        self.vision_base = (settings.dgx_vision_base_url or settings.dgx_text_base_url).rstrip("/")
        _refuse_paid_hosts(self.text_base)
        _refuse_paid_hosts(self.vision_base)
        self.text_model = settings.dgx_text_model
        self.vision_model = settings.dgx_vision_model
        token = _read_token(settings.dgx_api_key_file)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            transport=transport, headers=headers, timeout=httpx.Timeout(600.0, connect=10.0)
        )

    def check_body(self, body: dict[str, Any]) -> None:
        for msg in body["input"]:
            for part in msg["content"]:
                if part["type"] in ("input_audio", "input_file"):
                    raise UnsupportedModalityError(f"dgx backend cannot carry {part['type']} parts")

    @staticmethod
    def _messages(body: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        messages: list[dict[str, Any]] = []
        has_images = False
        if body.get("instructions"):
            messages.append({"role": "system", "content": body["instructions"]})
        for msg in body["input"]:
            parts = []
            for part in msg["content"]:
                if part["type"] == "input_text":
                    parts.append({"type": "text", "text": part["text"]})
                elif part["type"] == "input_image":
                    has_images = True
                    parts.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
            role = "system" if msg["role"] == "developer" else msg["role"]
            messages.append({"role": role, "content": parts})
        return messages, has_images

    async def send(self, data: bytes, body: dict[str, Any], res: Reservation) -> AdapterResult:
        self.check_body(body)
        messages, has_images = self._messages(body)
        base, model = (self.vision_base, self.vision_model) if has_images else (self.text_base, self.text_model)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": body["max_output_tokens"],
            "reasoning_effort": _EFFORT_MAP[body["reasoning"]["effort"]],
            "stream": False,
        }
        if "temperature" in body:
            payload["temperature"] = body["temperature"]
        response = await self._client.post(f"{base}/chat/completions", json=payload)
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        try:
            raw = response.json()
        except ValueError:
            raw = None
        if response.status_code >= 300 or not isinstance(raw, dict):
            return AdapterResult(response.status_code, raw if isinstance(raw, dict) else None, {}, retry_after)

        choice = (raw.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        u = raw.get("usage") or {}
        usage = None
        if isinstance(u.get("prompt_tokens"), int) and isinstance(u.get("completion_tokens"), int):
            usage = {
                "input_tokens": u["prompt_tokens"],
                "input_tokens_details": {"cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0},
                "output_tokens": u["completion_tokens"],
                "output_tokens_details": {
                    "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
                },
                "total_tokens": u["prompt_tokens"] + u["completion_tokens"],
            }
        normalized = {
            "id": raw.get("id"),
            "object": "response",
            "model": raw.get("model", model),
            "status": "completed" if choice.get("finish_reason") != "length" else "incomplete",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
            "usage": usage,
        }
        return AdapterResult(response.status_code, normalized, {}, retry_after)

    async def aclose(self) -> None:
        await self._client.aclose()
