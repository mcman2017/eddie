"""Offset-preserving comment/string masking for Lean 4 source.

`lex(text)` returns two masked copies of `text` with identical length and identical
newline positions:

* `code`: comments AND string/char literals replaced by spaces. Structural scans use it,
  so `theorem` inside a comment and `:=` inside a string can never match.
* `nocomment`: only comments blanked. Signatures are sliced from it, so a string literal
  in a theorem statement stays readable.

Handled: nested `/- ... -/` (with depth), docstrings `/-- -/` and module docs `/-! -/`
(their spans are recorded), `--` line comments, `"..."` with escapes (possibly multi-line),
raw strings `r"..."` / `r#"..."#`, char literals such as `'a'`, `'"'`, `'\\n'`. Primes in
identifiers (`h'`) are left alone. A UTF-8 BOM is treated as whitespace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NORMAL = re.compile(r"/-|--|\"|(?<![\w'!?.])r#*\"|'|﻿")
_BLOCK = re.compile(r"/-|-/")
_STRING_TAIL = re.compile(r'(?:[^"\\]|\\.)*"', re.S)
_CHAR_LIT = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|.)|[^'\\\n])'")
_NOT_NEWLINE = re.compile(r"[^\n]")


@dataclass(frozen=True)
class Span:
    start: int
    end: int  # exclusive
    kind: str  # "doc" | "moddoc" | "comment" | "line" | "string" | "char"


@dataclass
class LexResult:
    code: str
    nocomment: str
    docs: list[Span] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def _blank(segment: str) -> str:
    return _NOT_NEWLINE.sub(" ", segment)


def lex(text: str) -> LexResult:
    code_parts: list[str] = []
    nc_parts: list[str] = []
    docs: list[Span] = []
    anomalies: list[str] = []
    pos = 0
    n = len(text)

    def keep(upto: int) -> None:
        seg = text[pos:upto]
        code_parts.append(seg)
        nc_parts.append(seg)

    while pos < n:
        m = _NORMAL.search(text, pos)
        if m is None:
            keep(n)
            pos = n
            break
        start, tok = m.start(), m.group()
        if tok == "'":
            lit = _CHAR_LIT.match(text, start)
            prev = text[start - 1] if start else ""
            if lit is None or (prev and (prev.isalnum() or prev in "_'!?.₀₁₂₃₄₅₆₇₈₉")):
                keep(start + 1)  # a prime, part of an identifier
                pos = start + 1
                continue
            keep(start)
            blank = _blank(lit.group())
            code_parts.append(blank)
            nc_parts.append(lit.group())
            pos = lit.end()
            continue
        if tok == "﻿":
            keep(start)
            code_parts.append(" ")
            nc_parts.append(" ")
            pos = start + 1
            continue

        keep(start)
        if tok == "/-":
            kind = "doc" if text.startswith("/--", start) else "moddoc" if text.startswith("/-!", start) else "comment"
            depth, cursor, end = 0, start, None
            for b in _BLOCK.finditer(text, start):
                if b.start() < cursor:
                    continue
                if b.group() == "/-":
                    depth += 1
                else:
                    depth -= 1
                cursor = b.end()
                if depth == 0:
                    end = b.end()
                    break
            if end is None:
                anomalies.append("unterminated block comment")
                end = n
            blank = _blank(text[start:end])
            code_parts.append(blank)
            nc_parts.append(blank)
            if kind in ("doc", "moddoc"):
                docs.append(Span(start, end, kind))
            pos = end
        elif tok == "--":
            eol = text.find("\n", start)
            end = n if eol < 0 else eol
            blank = _blank(text[start:end])
            code_parts.append(blank)
            nc_parts.append(blank)
            pos = end
        elif tok == '"':
            tail = _STRING_TAIL.match(text, start + 1)
            if tail is None:
                anomalies.append("unterminated string literal")
                end = n
            else:
                end = tail.end()
            code_parts.append(_blank(text[start:end]))
            nc_parts.append(text[start:end])
            pos = end
        else:  # raw string r"..." / r#"..."#
            hashes = tok.count("#")
            closer = '"' + "#" * hashes
            close_at = text.find(closer, m.end())
            if close_at < 0:
                anomalies.append("unterminated raw string")
                end = n
            else:
                end = close_at + len(closer)
            code_parts.append(_blank(text[start:end]))
            nc_parts.append(text[start:end])
            pos = end

    code = "".join(code_parts)
    nocomment = "".join(nc_parts)
    assert len(code) == len(text) == len(nocomment)
    return LexResult(code=code, nocomment=nocomment, docs=docs, anomalies=anomalies)
