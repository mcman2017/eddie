import base64

import pytest

from eddie.llm.body import build_body
from eddie.llm.errors import ForbiddenParameterError
from eddie.llm.estimate import (
    FIXED_OVERHEAD_TOKENS,
    estimate_body,
    image_dimensions,
    image_tokens_upper,
    text_tokens_upper,
)
from eddie.llm.types import EddieRequest
from tests.helpers import make_jpeg_header, make_png


def _req(*parts, **kw):
    return EddieRequest.create(
        brain_region=kw.pop("region", "R2.critic"),
        effort=kw.pop("effort", "medium"),
        max_output_tokens=kw.pop("m", 256),
        messages=[{"role": "user", "content": list(parts)}],
        **kw,
    )


LEAN_DENSE = "theorem riemannHypothesis (s : ℂ) (h : riemannZeta s = 0) : ∀ ε > 0, ∃ δ, 𝕜 ⊆ 𝓝 ζ ∑ ∏ ⟨⟩ ⦃⦄"


@pytest.mark.parametrize(
    "text",
    [
        "plain ascii prompt",
        LEAN_DENSE,
        "黎曼猜想 的 形式化 证明",
        "ﷺ ﬁ ㎒ ℌ",  # NFKC expands these
        "    \n" * 50,
    ],
)
def test_text_bound_is_at_least_raw_and_nfkc_utf8_bytes(text):
    import unicodedata

    bound = text_tokens_upper(text)
    assert bound >= len(text.encode())
    assert bound >= len(unicodedata.normalize("NFKC", text).encode())


def test_four_byte_math_symbols_counted_at_four_tokens_each():
    assert text_tokens_upper("𝕜𝓝𝔼") == 12


def test_estimate_includes_overhead_and_every_text_field():
    req = _req({"type": "text", "text": "hello"}, instructions="be precise", text_format={"format": {"type": "json_schema", "name": "x", "schema": {"type": "object"}}})
    est = estimate_body(build_body(req))
    assert est.bounded
    assert est.input_tokens > FIXED_OVERHEAD_TOKENS + len("hello") + len("be precise")
    assert est.text_tokens >= len("hello") + len("be precise") + len("json_schema")


def test_png_and_jpeg_dimensions_from_headers():
    assert image_dimensions(make_png(640, 480), "image/png") == (640, 480)
    assert image_dimensions(make_jpeg_header(1280, 960), "image/jpeg") == (1280, 960)


def test_image_bound_is_generous():
    # 1280x960 -> 3x2 tiles; documented scaling is ~1,300-1,500 tokens
    assert image_tokens_upper(1280, 960) == 6 * 1_600 + 256


def test_image_part_estimate():
    png = make_png(512, 512)
    est = estimate_body(build_body(_req({"type": "image", "data": png, "mime": "image/png"})))
    assert est.image_tokens == 1_600 + 256


@pytest.mark.parametrize("w,h", [(10_000, 1), (4_097, 100), (9_000, 1_000)])
def test_extreme_images_are_refused(w, h):
    with pytest.raises(ForbiddenParameterError):
        image_tokens_upper(w, h)


def test_mime_magic_mismatch_is_refused():
    with pytest.raises(ForbiddenParameterError):
        image_dimensions(make_jpeg_header(64, 64), "image/png")
    with pytest.raises(ForbiddenParameterError):
        image_dimensions(make_png(64, 64), "image/jpeg")


@pytest.mark.parametrize("mime", ["image/gif", "image/svg+xml", "image/webp"])
def test_other_image_types_are_refused(mime):
    body = build_body(_req({"type": "text", "text": "x"}))
    body["input"][0]["content"].append(
        {"type": "input_image", "image_url": f"data:{mime};base64," + base64.b64encode(b"GIF89a....").decode()}
    )
    with pytest.raises(ForbiddenParameterError):
        estimate_body(body)


def test_unbounded_parts_are_reported():
    req = _req(
        {"type": "text", "text": "listen"},
        {"type": "audio", "data": b"RIFF....WAVE", "format": "wav"},
        {"type": "pdf", "data": b"%PDF-1.5", "filename": "remark.pdf"},
        {"type": "image_url", "url": "https://example.invalid/plot.png"},
    )
    est = estimate_body(build_body(req))
    assert not est.bounded
    assert len(est.unbounded) == 3


def test_invalid_base64_data_url_is_refused():
    body = build_body(_req({"type": "text", "text": "x"}))
    body["input"][0]["content"].append({"type": "input_image", "image_url": "data:image/png;base64,@@@"})
    with pytest.raises(ForbiddenParameterError):
        estimate_body(body)
