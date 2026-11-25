from eddie.ingest.lean_parser import parse_source_with_code
from eddie.ingest.refgraph import NameTable, decl_edges, parse_opens, resolve

NAMES = [
    ("A.B.target", 1),
    ("A.helper", 2),
    ("topLevel", 3),
    ("Opened.thing", 4),
    ("Struct.uniqueProjectionName", 5),
    ("X.dup_name", 6),
    ("Y.dup_name", 7),
    ("A.B.C.caller", 8),
]
TABLE = NameTable.from_pairs(NAMES)


def test_namespace_chain_resolution():
    ns = ["A", "B", "C"]
    assert resolve("target", ns, [], TABLE) == (1, False)  # A.B.target via A.B
    assert resolve("helper", ns, [], TABLE) == (2, False)  # A.helper via A
    assert resolve("topLevel", ns, [], TABLE) == (3, False)
    assert resolve("B.target", ["A"], [], TABLE) == (1, False)  # partially qualified


def test_open_root_and_explicit_forms():
    assert resolve("thing", [], ["Opened"], TABLE) == (4, False)
    assert resolve("_root_.topLevel", ["A"], [], TABLE) == (3, False)
    assert resolve("@A.helper", [], [], TABLE) == (2, False)


def test_dot_notation_and_weak_unique_suffix():
    assert resolve("h.helper", ["A"], [], TABLE) == (2, False)  # local head dropped
    assert resolve("x.uniqueProjectionName", [], [], TABLE) == (5, True)  # unique last component
    assert resolve("dup_name", [], [], TABLE) == (None, False)  # ambiguous: never guessed


def test_keywords_and_locals_do_not_resolve():
    for tok in ("by", "exact", "simp", "h", "hx", "n"):
        assert resolve(tok, ["A"], [], TABLE) == (None, False)


def test_parse_opens_variants():
    ctx = "open A B; variable (x : ℕ); open scoped C; open D in; open E (f g); open F hiding h"
    assert parse_opens(ctx) == ["A", "B", "C", "D", "E", "F"]
    assert parse_opens(None) == []


def test_decl_edges_ignore_comments_strings_and_self():
    src = (
        "namespace A.B.C\n\n"
        "theorem caller : True := by\n"
        "  -- target is mentioned in a comment only\n"
        '  have : "helper" = "helper" := rfl\n'
        "  exact (target.trans (_root_.topLevel)) caller\n"
        "end A.B.C\n"
    )
    rec, code = parse_source_with_code("x.lean", src.encode())
    d = rec.decls[0]
    edges = dict(decl_edges(code, d.start_char, d.end_char, d.full_name.split(".")[:-1], [], TABLE, self_id=8))
    assert edges == {1: False, 3: False}  # target (in code), topLevel; not helper (string), not self
