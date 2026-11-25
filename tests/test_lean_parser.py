"""Structural parser on synthetic fixtures only (no archive text ever lives in the repo)."""

from __future__ import annotations

import pytest

from eddie.ingest.lean_parser import parse_source
from eddie.ingest.synthetic import SAMPLE_FILES

EDGE = SAMPLE_FILES["src/BuildAid/Edge.lean"]


@pytest.fixture(scope="module")
def edge():
    return parse_source("src/BuildAid/Edge.lean", EDGE.encode())


def by_name(rec, name):
    return next(d for d in rec.decls if d.full_name == name)


def test_imports_with_module_system_modifiers(edge):
    assert [(i.module, i.public, i.meta, i.all) for i in edge.imports] == [
        ("Formalism.Basic", True, False, False),
        ("Lemmata.Bounds", False, False, True),
        ("Lemmata.Bounds", True, True, False),
    ]


def test_keywords_inside_comments_and_strings_are_not_declarations(edge):
    names = {d.full_name for d in edge.decls}
    assert not {"fake", "hidden", "alsoHidden", "Edge.fake", "Edge.hidden"} & names
    assert edge.anomalies == []


def test_multiline_signature_with_nested_brackets_unicode_and_default_args(edge):
    d = by_name(edge, "Edge.tricky_signature")
    assert d.signature.startswith("theorem tricky_signature {α : Type*} [Ring α] (f : α → α)")
    assert "(h₂ : ∀ ⦃y : α⦄, f (f y) = y := by intro y; simp [h₁])" in d.signature
    assert "(n : ℕ := 3)" in d.signature
    assert d.signature.endswith("∃ g : α → α, ∀ x, g (f x) = x ∧ (⟨n, n⟩ : ℕ × ℕ).1 = n")
    assert "trailing comment" not in d.signature  # comments stripped
    assert d.body_kind == "by"
    assert d.modifiers == ("protected",)
    assert d.attributes == "@[simp, norm_cast]"
    assert d.docstring == "A docstring with `:=` inside and a { brace."
    assert "set_option maxHeartbeats 400000 in" in d.context


def test_tactic_bodies_are_stripped_but_recoverable_by_offsets(edge):
    data = EDGE.encode()
    d = by_name(edge, "Edge.tricky_signature")
    full = data[d.start_byte : d.end_byte].decode()
    assert full.startswith("@[simp, norm_cast]\nprotected theorem tricky_signature")
    assert full.rstrip().endswith("simp [h₁]")
    assert "refine" not in d.signature
    assert d.body_bytes == d.end_byte - d.sig_end_byte > 0


def test_strings_and_char_literals_do_not_break_signatures(edge):
    assert by_name(edge, "Edge.string_with_assign").signature == 'lemma string_with_assign : "a := b".length = 6'
    assert by_name(edge, "Edge.char_literal").signature == "theorem char_literal : '\"'.toNat = 34"
    assert by_name(edge, "Edge.raw_string").body_kind == "by"


def test_names_quoted_universe_root_and_namespaces(edge):
    assert by_name(edge, "Edge.«weird name with spaces»").kind == "theorem"
    u = by_name(edge, "Edge.universe_params")
    assert u.universe_params == "{u, v}"
    root = by_name(edge, "rootLevel")
    assert root.namespace == "Edge" and root.name == "_root_.rootLevel"
    assert by_name(edge, "Edge.in_noncomputable_section").namespace == "Edge"


def test_open_in_prefix_applies_only_to_next_decl(edge):
    assert by_name(edge, "Edge.uses_open").context.endswith("open Formalism in")
    assert "open Formalism in" not in by_name(edge, "Edge.pattern_match").context


def test_body_kinds_instances_axioms_examples(edge):
    assert by_name(edge, "Edge.pattern_match").body_kind == "match"
    inst = by_name(edge, "Edge.instSpectral")
    assert inst.kind == "instance" and inst.signature.startswith("instance (priority := 100) instSpectral")
    assert by_name(edge, "Edge.synthetic_axiom").body_kind == "none"
    assert by_name(edge, "Edge.hidden_constant").kind == "opaque"
    ex = [d for d in edge.decls if d.kind == "example"]
    assert len(ex) == 1 and ex[0].name is None


def test_sorry_detection(edge):
    assert by_name(edge, "Edge.with_sorry").has_sorry
    assert not by_name(edge, "Edge.tricky_signature").has_sorry


def test_mutual_block_indented_decls(edge):
    assert {"Edge.isEven", "Edge.isOdd"} <= {d.full_name for d in edge.decls}


def test_anonymous_instance_and_where_structures():
    rec = parse_source("src/Formalism/Basic.lean", SAMPLE_FILES["src/Formalism/Basic.lean"].encode())
    kinds = {(d.kind, d.full_name, d.body_kind) for d in rec.decls}
    assert ("structure", "Formalism.SpectralDatum", "where") in kinds
    assert ("class", "Formalism.HasCriticalLine", "where") in kinds
    assert ("instance", None, "term") in kinds
    assert ("abbrev", "Formalism.CriticalStrip", "term") in kinds
    sd = next(d for d in rec.decls if d.full_name == "Formalism.SpectralDatum")
    assert sd.signature == "structure SpectralDatum (α : Type u) [NormedAddCommGroup α]"
    assert "universe u" in sd.context


def test_attributes_on_their_own_line_merge_with_the_decl():
    src = "namespace N\n\n@[simp]\ntheorem a : True := trivial\n\n@[ext, simp]\nprivate lemma b : 1 = 1 := rfl\nend N\n"
    rec = parse_source("x.lean", src.encode())
    assert [(d.full_name, d.attributes, d.modifiers) for d in rec.decls] == [
        ("N.a", "@[simp]", ()),
        ("N.b", "@[ext, simp]", ("private",)),
    ]


def test_multiline_variable_prefix_with_in():
    src = "variable {a : ℕ}\n    (h : a = a) in\ntheorem t : a = a := h\ntheorem u : True := trivial\n"
    rec = parse_source("x.lean", src.encode())
    t, u = rec.decls
    assert "variable {a : ℕ} (h : a = a) in" in t.context
    assert "in" not in u.context.split()


@pytest.mark.parametrize(
    "src,count",
    [
        (b"", 0),
        (b"-- only a comment\n/- and a block -/\n", 0),
        (b"\xef\xbb\xbfimport A\ntheorem t : True := trivial\n", 1),  # BOM
        (b"import A\r\ntheorem t : True := by\r\n  trivial\r\n", 1),  # CRLF
    ],
)
def test_degenerate_files(src, count):
    rec = parse_source("x.lean", src)
    assert len(rec.decls) == count
    assert rec.anomalies == []


def test_crlf_and_bom_offsets_round_trip():
    src = "﻿import A\r\n\r\ntheorem ζ_bound (s : ℂ) : ‖s‖ ≥ 0 := by\r\n  positivity\r\n".encode()
    d = parse_source("x.lean", src).decls[0]
    assert src[d.start_byte : d.end_byte].decode().startswith("theorem ζ_bound")
    assert d.signature == "theorem ζ_bound (s : ℂ) : ‖s‖ ≥ 0"


def test_every_sample_file_parses_cleanly():
    total = 0
    for rel, text in SAMPLE_FILES.items():
        rec = parse_source(rel, text.encode())
        assert rec.anomalies == [], rel
        total += len(rec.decls)
        for d in rec.decls:
            assert text.encode()[d.start_byte : d.end_byte].decode().strip()
    assert total >= 40


def test_main_roots_and_imports():
    rec = parse_source("src/Main.lean", SAMPLE_FILES["src/Main.lean"].encode())
    assert [i.module for i in rec.imports] == ["Statements", "Formalism.Zeta", "Development.Spectral.Trace"]
    assert [d.full_name for d in rec.decls] == ["riemannHypothesis", "riemannZeta_eq_zero_re"]
    assert rec.module_doc.startswith("# Main")


def test_dotted_namespace_opens_one_scope_per_component():
    src = (
        "noncomputable section\n\nnamespace A.B.C\n\ndef inner : ℕ := 1\n\nend C\n\n"
        "theorem middle : True := trivial\n\nend A.B\n\nend\n"
    )
    rec = parse_source("x.lean", src.encode())
    assert [d.full_name for d in rec.decls] == ["A.B.C.inner", "A.B.middle"]
    assert rec.anomalies == []
