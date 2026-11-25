"""$0 structural parser for Lean 4 source files.

It extracts imports, declaration headers (kind, name, fully qualified name, modifiers,
attributes, docstring, signature) and the active `open`/`variable` context, and strips
proof bodies while keeping exact UTF-8 byte offsets, so a body can be loaded on demand.

The parser is regex-driven over the lexer's masked text: comments and strings can never
produce false matches, and every offset stays valid. Commands start at column 0, which is
how the archive is written; indented declarations are accepted inside `mutual` blocks.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field

from eddie.ingest.lexer import lex

PARSER_VERSION = "2"

DECL_KINDS = frozenset(
    {"theorem", "lemma", "def", "abbrev", "instance", "structure", "class", "inductive", "axiom", "opaque", "example"}
)
MODIFIERS = ("private", "protected", "public", "noncomputable", "partial", "unsafe", "nonrec", "meta")
PREFIXABLE = frozenset({"open", "omit", "include", "set_option", "variable", "attribute"})
OTHER_COMMANDS = (
    "namespace", "end", "section", "mutual", "open", "variable", "universe", "set_option", "attribute",
    "omit", "include", "local", "scoped", "notation", "infix", "infixl", "infixr", "prefix", "postfix",
    "macro", "macro_rules", "syntax", "elab", "elab_rules", "declare_syntax_cat", "initialize",
    "builtin_initialize", "export", "alias", "deriving", "register_option", "add_decl_doc",
)

_KW_ALT = "|".join(sorted(DECL_KINDS | set(OTHER_COMMANDS) | set(MODIFIERS), key=len, reverse=True))
_COL0_CMD = re.compile(rf"^(?:@\[|#[A-Za-z_]+\b|(?:{_KW_ALT})(?![\w'!?]))", re.M)
_INDENTED_DECL = re.compile(
    rf"^[ \t]+(?:(?:{'|'.join(MODIFIERS)})\s+)*(?:{'|'.join(sorted(DECL_KINDS))})(?![\w'!?])", re.M
)
_MODIFIER = re.compile(rf"({'|'.join(MODIFIERS)})(?=\s)")
_KEYWORD = re.compile(rf"(#[A-Za-z_]+|{_KW_ALT})(?![\w'!?])")
_COMPONENT = r"(?:«[^»\n]*»|[^\s(){}\[\]⦃⦄⟨⟩⟦⟧:,«».@$]+)"
_NAME = re.compile(rf"{_COMPONENT}(?:\.{_COMPONENT})*")
_SIG_TOKEN = re.compile(r"[(\[{⦃⟨⟦]|[)\]}⦄⟩⟧]|:=|(?<![\w.'])where(?![\w'!?])|\n[ \t]*\|(?!\|)")
_SORRY = re.compile(r"(?<![\w.'])(?:sorry|sorryAx|admit)(?![\w'!?])")
_ENDS_WITH_IN = re.compile(r"(?<![\w'])in\s*$")
_IMPORT_LINE = re.compile(r"^[ \t]*((?:(?:public|private|meta)[ \t]+)*)import[ \t]+(.*)$")
_WS = re.compile(r"\s+")
_OPENERS, _CLOSERS = "([{⦃⟨⟦", ")]}⦄⟩⟧"


@dataclass(frozen=True)
class Import:
    module: str
    public: bool = False
    meta: bool = False
    all: bool = False


@dataclass
class Decl:
    kind: str
    name: str | None
    full_name: str | None
    namespace: str
    modifiers: tuple[str, ...]
    attributes: str | None
    docstring: str | None
    signature: str
    start_byte: int
    sig_end_byte: int
    end_byte: int
    start_line: int
    end_line: int
    body_kind: str  # by | term | where | match | none
    body_bytes: int
    has_sorry: bool
    context: str
    universe_params: str | None = None
    # character offsets into the decoded source (in-memory only; not stored in the index)
    start_char: int = -1
    sig_end_char: int = -1
    end_char: int = -1


@dataclass
class ModuleRecord:
    path: str
    sha256: str
    bytes: int
    lines: int
    imports: list[Import]
    module_doc: str | None
    decls: list[Decl]
    anomalies: list[str] = field(default_factory=list)


class _Offsets:
    """char offset -> (UTF-8 byte offset, 1-based line), computed per line."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.ascii = text.isascii()
        self.line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
        if not self.ascii:
            acc, self.byte_starts = 0, []
            for i, start in enumerate(self.line_starts):
                self.byte_starts.append(acc)
                end = self.line_starts[i + 1] if i + 1 < len(self.line_starts) else len(text)
                acc += len(text[start:end].encode("utf-8", "surrogatepass"))

    def line(self, pos: int) -> int:
        return bisect.bisect_right(self.line_starts, pos)

    def byte(self, pos: int) -> int:
        if self.ascii:
            return pos
        i = bisect.bisect_right(self.line_starts, pos) - 1
        return self.byte_starts[i] + len(self.text[self.line_starts[i] : pos].encode("utf-8", "surrogatepass"))


def _skip_ws(s: str, i: int, end: int) -> int:
    while i < end and s[i].isspace():
        i += 1
    return i


def _rstrip_pos(s: str, i: int, floor: int) -> int:
    while i > floor and s[i - 1].isspace():
        i -= 1
    return i


def _match_bracket(s: str, i: int, end: int) -> int:
    """Index just past the bracket closing the one at s[i]."""
    depth = 0
    while i < end:
        ch = s[i]
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return end


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _parse_imports(code: str) -> tuple[list[Import], int, bool]:
    imports: list[Import] = []
    pos = 0
    is_module = False
    for line_match in re.finditer(r"[^\n]*(?:\n|$)", code):
        line = line_match.group()
        stripped = line.strip()
        if not stripped:
            if line_match.end() >= len(code):
                break
            continue
        if stripped in ("module", "prelude"):
            is_module = is_module or stripped == "module"
            pos = line_match.end()
            continue
        m = _IMPORT_LINE.match(line.rstrip("\n"))
        if not m:
            break
        mods = m.group(1).split()
        flags = {"public": "public" in mods, "meta": "meta" in mods, "all": False}
        for tok in m.group(2).split():
            if tok in ("all", "runtime"):
                flags["all"] = flags["all"] or tok == "all"
                continue
            if tok in ("public", "meta", "private"):
                flags[tok if tok != "private" else "public"] = tok != "private"
                continue
            if tok == "import":
                flags = {"public": False, "meta": False, "all": False}
                continue
            imports.append(Import(tok, flags["public"], flags["meta"], flags["all"]))
        pos = line_match.end()
    return imports, pos, is_module


def parse_source(path: str, data: bytes) -> ModuleRecord:
    return parse_source_with_code(path, data)[0]


def parse_source_with_code(path: str, data: bytes) -> tuple[ModuleRecord, str]:
    """Parse, and also return the lexer's masked `code` text (comments/strings blanked)."""
    anomalies: list[str] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", "replace")
        anomalies.append("invalid UTF-8 (byte offsets approximate)")
    lexed = lex(text)
    anomalies += lexed.anomalies
    code, nocomment = lexed.code, lexed.nocomment
    n = len(code)
    offsets = _Offsets(text)
    imports, header_end, _ = _parse_imports(code)

    module_doc = None
    for span in lexed.docs:
        if span.kind == "moddoc":
            module_doc = _norm(text[span.start + 3 : span.end - 2])
            break
    doc_spans = [s for s in lexed.docs if s.kind == "doc"]
    doc_ends = [s.end for s in doc_spans]

    starts = [m.start() for m in _COL0_CMD.finditer(code, header_end)]
    # indented declarations are commands only inside `mutual` blocks
    mutual_starts = [s for s in starts if code.startswith("mutual", s)]
    if mutual_starts:
        extra = []
        for ms in mutual_starts:
            nxt = next((s for s in starts if s > ms), n)
            extra += [m.start() + (len(m.group()) - len(m.group().lstrip())) for m in _INDENTED_DECL.finditer(code, ms, nxt)]
        starts = sorted(set(starts) | set(extra))

    scopes: list[tuple[str, str | None, list[str]]] = []  # (kind, name, context items)
    decls: list[Decl] = []
    prefixes: list[str] = []
    prev_content_end = header_end
    i = 0
    while i < len(starts):
        s = starts[i]
        q = s
        attrs: list[str] = []
        mods: list[str] = []
        while True:
            q = _skip_ws(code, q, n)
            if code.startswith("@[", q):
                r = _match_bracket(code, q + 1, n)
                attrs.append(_norm(nocomment[q:r]))
                q = r
                continue
            m = _MODIFIER.match(code, q)
            if m:
                mods.append(m.group(1))
                q = m.end()
                continue
            break
        kw_match = _KEYWORD.match(code, q)
        j = i
        while j + 1 < len(starts) and starts[j + 1] <= q:
            j += 1  # header chain (attributes / modifiers) spilled over later "starts"
        extent_end = starts[j + 1] if j + 1 < len(starts) else n
        content_end = _rstrip_pos(code, extent_end, q)
        i = j + 1
        if kw_match is None:
            if attrs or mods:
                anomalies.append(f"unparsed command at line {offsets.line(s)}")
            continue
        kw = kw_match.group(1)
        cmd_text = code[q:content_end]

        if kw in PREFIXABLE and _ENDS_WITH_IN.search(cmd_text):
            prefixes.append(_norm(nocomment[q:content_end]))
            continue
        if kw in ("namespace", "section"):
            # `namespace A.B.C` opens three nested scopes; `end C` closes one, `end A.B` two.
            words = cmd_text[len(kw) :].strip().split()
            parts = words[0].split(".") if words else [None]
            for part in parts:
                scopes.append((kw, part, []))
            prefixes = []
        elif kw == "mutual":
            scopes.append(("mutual", None, []))
        elif kw == "end":
            words = cmd_text[len("end") :].strip().split()
            depth = len(words[0].split(".")) if words else 1
            if len(scopes) < depth:
                anomalies.append(f"`end` without an open scope at line {offsets.line(s)}")
            del scopes[max(0, len(scopes) - depth) :]
            prefixes = []
        elif kw in ("open", "variable", "universe", "omit", "include"):
            if scopes:
                scopes[-1][2].append(_norm(nocomment[q:content_end]))
            else:
                scopes.append(("file", None, [_norm(nocomment[q:content_end])]))
            prefixes = []
        elif kw in DECL_KINDS:
            decls.append(
                _parse_decl(
                    kw, mods, attrs, s, q, kw_match.end(), content_end, code, nocomment, text, offsets,
                    scopes, prefixes, doc_spans, doc_ends, prev_content_end,
                )
            )
            prefixes = []
        else:
            prefixes = []
        prev_content_end = content_end

    record = ModuleRecord(
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        lines=text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        imports=imports,
        module_doc=module_doc,
        decls=decls,
        anomalies=anomalies,
    )
    return record, code


def _parse_decl(
    kw, mods, attrs, chain_start, kw_pos, kw_end, content_end, code, nocomment, text, offsets, scopes,
    prefixes, doc_spans, doc_ends, prev_content_end,
) -> Decl:
    q = _skip_ws(code, kw_end, content_end)
    name = None
    universe = None
    if kw == "instance" and code.startswith("(", q):
        q = _skip_ws(code, _match_bracket(code, q, content_end), content_end)  # (priority := n)
    if kw != "example":
        m = _NAME.match(code, q, content_end)
        if m and m.group() != "where" and not (kw == "instance" and code[q] in ":[({⦃"):
            name = m.group()
            q = m.end()
            if code.startswith(".{", q):
                close = code.find("}", q)
                universe = code[q + 1 : close + 1] if close != -1 else None
                q = close + 1 if close != -1 else q

    depth = 0
    sig_end = content_end
    body_kind = "none"
    for tok in _SIG_TOKEN.finditer(code, q, content_end):
        t = tok.group()
        if t in _OPENERS:
            depth += 1
        elif t in _CLOSERS:
            depth = max(0, depth - 1)
        elif depth == 0:
            sig_end = tok.start()
            if t == ":=":
                nxt = _skip_ws(code, tok.end(), content_end)
                body_kind = "by" if re.match(r"by(?![\w'!?])", code[nxt : nxt + 3]) else "term"
            elif t == "where":
                body_kind = "where"
            else:
                body_kind = "match"
            break
    sig_end = _rstrip_pos(code, sig_end, kw_pos)

    namespace = ".".join(name_ for kind, name_, _ in scopes if kind == "namespace" and name_)
    full_name = None
    if name is not None:
        if name.startswith("_root_."):
            full_name = name[len("_root_.") :]
        else:
            full_name = f"{namespace}.{name}" if namespace else name

    doc = None
    k = bisect.bisect_right(doc_ends, chain_start) - 1
    if k >= 0 and doc_spans[k].start >= prev_content_end:
        span = doc_spans[k]
        doc = _norm(text[span.start + 3 : span.end - 2])

    context_items = [item for _, _, items in scopes for item in items] + list(prefixes)
    context = "; ".join(context_items)
    if len(context) > 2_000:
        context = context[:2_000] + " …"

    has_sorry = bool(_SORRY.search(code, sig_end, content_end))
    start_byte = offsets.byte(chain_start)  # includes attributes and modifiers
    sig_end_byte = offsets.byte(sig_end)
    end_byte = offsets.byte(content_end)
    return Decl(
        kind=kw,
        name=name,
        full_name=full_name,
        namespace=namespace,
        modifiers=tuple(mods),
        attributes=" ".join(attrs) if attrs else None,
        docstring=doc,
        signature=_norm(nocomment[kw_pos:sig_end]),
        start_byte=start_byte,
        sig_end_byte=sig_end_byte,
        end_byte=end_byte,
        start_line=offsets.line(chain_start),
        end_line=offsets.line(max(content_end - 1, kw_pos)),
        body_kind=body_kind,
        body_bytes=end_byte - sig_end_byte,
        has_sorry=has_sorry,
        context=context,
        universe_params=universe,
        start_char=chain_start,
        sig_end_char=sig_end,
        end_char=content_end,
    )


def parse_file(path: str, root: str | None = None) -> ModuleRecord:
    from pathlib import Path

    p = Path(path)
    rel = str(p.relative_to(root)) if root else str(p)
    return parse_source(rel, p.read_bytes())
