"""Upper-bound input-token estimator for `/v1/responses` bodies.

This is a provable bound, not a heuristic: byte-level and byte-fallback tokenizers never
emit more tokens than the UTF-8 bytes they consume. We take the larger of the raw and
NFKC-normalized byte counts (normalization can expand some characters), and add a
generous fixed allowance for hidden system and reasoning framing.

Images are bounded from their real header dimensions with a tile model about 7x the
documented scaling. Parts that cannot be bounded locally (audio, PDF, remote URLs) are
reported as `unbounded`. The governor refuses them unless the caller explicitly opts into
a worst-case reservation.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import struct
import unicodedata
from dataclasses import dataclass
from typing import Any

from eddie.llm.errors import ForbiddenParameterError

FIXED_OVERHEAD_TOKENS = 1_024
PER_MESSAGE_TOKENS = 16
PER_PART_TOKENS = 8

IMAGE_TILE_PX = 512
IMAGE_TOKENS_PER_TILE = 1_600
IMAGE_BASE_TOKENS = 256
MAX_IMAGE_SIDE = 4_096
MAX_IMAGE_ASPECT = 8.0
MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8"
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


@dataclass(frozen=True)
class Estimate:
    input_tokens: int  # upper bound over every bounded part (+ overhead)
    text_tokens: int
    image_tokens: int
    unbounded: tuple[str, ...] = ()

    @property
    def bounded(self) -> bool:
        return not self.unbounded


def text_tokens_upper(text: str) -> int:
    raw = len(text.encode("utf-8"))
    nfkc = len(unicodedata.normalize("NFKC", text).encode("utf-8"))
    return max(raw, nfkc)


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != PNG_MAGIC or data[12:16] != b"IHDR":
        raise ForbiddenParameterError("image is not a valid PNG")
    return struct.unpack(">II", data[16:24])


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if data[:2] != JPEG_MAGIC:
        raise ForbiddenParameterError("image is not a valid JPEG")
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            raise ForbiddenParameterError("corrupt JPEG marker stream")
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue  # standalone markers carry no length
        if marker == 0xD9:
            break
        if i + 2 > n:
            break
        (seg_len,) = struct.unpack(">H", data[i : i + 2])
        if seg_len < 2:
            raise ForbiddenParameterError("corrupt JPEG segment")
        if marker in _JPEG_SOF:
            if i + 7 > n:
                break
            height, width = struct.unpack(">HH", data[i + 3 : i + 7])
            return width, height
        i += seg_len
    raise ForbiddenParameterError("JPEG has no frame header")


def image_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    """Read width/height from the header; the magic bytes must match the declared MIME."""
    if len(data) > MAX_INLINE_IMAGE_BYTES:
        raise ForbiddenParameterError("inline image is too large")
    is_png, is_jpeg = data[:8] == PNG_MAGIC, data[:2] == JPEG_MAGIC
    if mime == "image/png":
        if not is_png:
            raise ForbiddenParameterError("declared image/png but bytes are not PNG")
        return _png_dimensions(data)
    if mime == "image/jpeg":
        if not is_jpeg:
            raise ForbiddenParameterError("declared image/jpeg but bytes are not JPEG")
        return _jpeg_dimensions(data)
    raise ForbiddenParameterError(f"image type {mime!r} is not allowed (PNG or JPEG only)")


def image_tokens_upper(width: int, height: int) -> int:
    if width < 1 or height < 1:
        raise ForbiddenParameterError("image has an empty dimension")
    if max(width, height) > MAX_IMAGE_SIDE:
        raise ForbiddenParameterError(f"image side {max(width, height)} exceeds {MAX_IMAGE_SIDE}px")
    if max(width, height) / min(width, height) > MAX_IMAGE_ASPECT:
        raise ForbiddenParameterError(f"image aspect ratio exceeds {MAX_IMAGE_ASPECT}:1")
    tiles = math.ceil(width / IMAGE_TILE_PX) * math.ceil(height / IMAGE_TILE_PX)
    return tiles * IMAGE_TOKENS_PER_TILE + IMAGE_BASE_TOKENS


def parse_data_url(url: str) -> tuple[str, bytes] | None:
    """Return (mime, bytes) for a base64 data URL, or None for a remote URL."""
    if not url.startswith("data:"):
        return None
    header, sep, payload = url[5:].partition(",")
    if not sep or ";base64" not in header:
        raise ForbiddenParameterError("data URLs must be base64-encoded")
    mime = header.split(";", 1)[0].strip().lower()
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ForbiddenParameterError("data URL payload is not valid base64") from exc
    return mime, data


def estimate_body(body: dict[str, Any]) -> Estimate:
    overhead = FIXED_OVERHEAD_TOKENS
    text = 0
    image = 0
    unbounded: list[str] = []

    if isinstance(body.get("instructions"), str):
        text += text_tokens_upper(body["instructions"])
    if "text" in body:  # structured-output schema is part of the prompt
        text += text_tokens_upper(json.dumps(body["text"], ensure_ascii=False, sort_keys=True))

    for msg in body.get("input", []):
        overhead += PER_MESSAGE_TOKENS
        text += text_tokens_upper(str(msg.get("role", "")))
        for part in msg.get("content", []):
            overhead += PER_PART_TOKENS
            kind = part.get("type")
            if kind == "input_text":
                text += text_tokens_upper(part.get("text", ""))
            elif kind == "input_image":
                parsed = parse_data_url(str(part.get("image_url", "")))
                if parsed is None:
                    unbounded.append("remote image URL (fetched server-side)")
                else:
                    mime, data = parsed
                    image += image_tokens_upper(*image_dimensions(data, mime))
            elif kind == "input_file":
                unbounded.append("file/PDF input (token formula undocumented)")
            elif kind == "input_audio":
                unbounded.append("audio input (token formula undocumented)")
            else:
                raise ForbiddenParameterError(f"content part type {kind!r} is not allowed")

    return Estimate(
        input_tokens=overhead + text + image,
        text_tokens=text,
        image_tokens=image,
        unbounded=tuple(unbounded),
    )
