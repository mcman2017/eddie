from eddie.ingest.lexer import lex


def test_masking_preserves_length_and_newlines():
    text = 'theorem a : "x\n:= y".length = 4 := by\n  -- hidden := 1\n  /- theorem b -/ rfl\n'
    r = lex(text)
    assert len(r.code) == len(text) == len(r.nocomment)
    assert [i for i, c in enumerate(r.code) if c == "\n"] == [i for i, c in enumerate(text) if c == "\n"]
    assert "hidden" not in r.code and "theorem b" not in r.code
    assert ":= y" not in r.code  # string content masked in `code`
    assert ":= y" in r.nocomment  # but kept in `nocomment`


def test_nested_block_comments():
    text = "a /- outer /- inner -/ still comment -/ b"
    r = lex(text)
    assert r.code.split() == ["a", "b"]


def test_doc_and_moddoc_spans_recorded():
    text = "/-! module doc -/\n/-- decl doc -/\ntheorem t : True := trivial\n"
    r = lex(text)
    kinds = [s.kind for s in r.docs]
    assert kinds == ["moddoc", "doc"]
    assert text[r.docs[1].start : r.docs[1].end] == "/-- decl doc -/"


def test_char_literals_vs_primes():
    text = "theorem t (h' : x = y) : '\"'.toNat = 34 ∧ f' x = '\\n'.toNat := rfl"
    r = lex(text)
    assert "h'" in r.code and "f'" in r.code
    assert '"' not in r.code  # the '"' char literal is masked, not mistaken for a string
    assert r.code.count("'") == 2  # only the two primes survive


def test_raw_strings():
    text = 'def s := r#"has "quotes" and := inside"# ++ r"plain"'
    r = lex(text)
    assert ":=" in r.code and r.code.count(":=") == 1
    assert "quotes" not in r.code


def test_line_comment_wins_over_later_block_open():
    r = lex("x -- comment /- not a block\ny")
    assert r.code.split() == ["x", "y"]
    assert not r.anomalies


def test_unterminated_comment_is_an_anomaly():
    r = lex("theorem t : True := /- oops")
    assert r.anomalies == ["unterminated block comment"]


def test_bom_is_whitespace():
    r = lex("﻿import A")
    assert r.code == " import A"
