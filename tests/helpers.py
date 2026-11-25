"""Shared test helpers: synthetic images and realistic Ember response bodies."""

from __future__ import annotations

import struct
import zlib
from typing import Any


def make_png(width: int, height: int, *, value: int = 128) -> bytes:
    """A valid grayscale 8-bit PNG built with zlib only."""
    raw = b"".join(b"\x00" + bytes([value]) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def make_jpeg_header(width: int, height: int) -> bytes:
    """Minimal JPEG marker stream (SOI, APP0, SOF0, EOI): enough for header parsing."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def ember_response(
    *,
    input_tokens: int = 120,
    cached_tokens: int = 0,
    output_tokens: int = 40,
    reasoning_tokens: int = 24,
    text: str = "ok",
    response_id: str = "resp_test_0001",
    status: str = "completed",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A realistic `/v1/responses` JSON body with Meta's nested usage field names."""
    if usage is None:
        usage = {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": input_tokens + output_tokens,
        }
    return {
        "id": response_id,
        "object": "response",
        "model": "muse-ember-1.0",
        "status": status,
        "incomplete_details": None,
        "output": [
            {
                "type": "reasoning",
                "id": "rs_test_0001",
                "summary": [{"type": "summary_text", "text": "considered the request"}],
                "status": "completed",
            },
            {
                "type": "message",
                "id": "msg_test_0001",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ],
        "usage": usage,
    }
