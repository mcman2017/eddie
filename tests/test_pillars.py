from collections import Counter

from eddie.planner.pillars import (
    K_MAX,
    K_MIN,
    assign_modules,
    camel_words,
    extract_pdf_text,
    humanize,
    match_timeline,
    partition,
    timeline_items,
)


def synthetic_live() -> dict[str, int]:
    live: dict[str, int] = {"Main": 5, "Statements": 4, "Supplements.Kernels.Core": 4}
    for i in range(8):  # one dominant subtree that must be split
        for j in range(30):
            live[f"Development.AbelianZone.Part{i}.Mod{j}"] = 200
    for name, mass in (("Development.SpectralZone", 9_000), ("Development.WeilBound", 7_000)):
        for j in range(20):
            live[f"{name}.Sub{j % 4}.M{j}"] = mass // 20
    for i in range(6):
        for j in range(10):
            live[f"Formalism.Topic{i}.M{j}"] = 300
    for i in range(3):
        live[f"GroundTheory.Field{i}.M"] = 2_500
    for i in range(5):  # slivers that must be folded
        live[f"Lemmata.Tiny{i}"] = 20
    live["BuildAid.Dead"] = 0
    return live


def test_partition_hits_the_pillar_range_and_balances_mass():
    live = synthetic_live()
    specs = partition(live)
    assert K_MIN <= len(specs) <= K_MAX
    assert specs[0].title == "Top-level reduction"
    total = sum(live.values())
    assert max(s.live for s in specs[1:]) <= 0.2 * total * 1.5
    assert sum(s.live for s in specs) == total
    names = list(live)
    assigned = assign_modules(names, specs)
    for name, p in zip(names, assigned, strict=True):
        if live[name] > 0:
            assert p >= 0, name
    assert assigned[names.index("Main")] == 0
    assert assigned[names.index("BuildAid.Dead")] == -1


def test_longest_prefix_wins():
    specs = partition(synthetic_live())
    a = assign_modules(["Development.AbelianZone.Part3.Mod7"], specs)[0]
    assert "Abelian Zone" in specs[a].title


def test_humanize_and_camel_words():
    assert humanize("AbelianVarietyTheory") == "Abelian Variety Theory"
    assert camel_words("GGPPeriodIntegrals") == ["ggp", "period", "integrals"]


TIMELINE_TEXT = """Some preamble with a footnote 1 https://example.invalid/x
1. 10/6/26: Alpha determinant.
2. 10/9/26: Beta lifts across a long widget chain, with a remark,
continuing onto a second line.
3. 10/19/26+: Gamma widget integral of gizmos; and more.
4. 11/15/26: Delta norm inequality.
Unrelated text 7. not part of the run
"""


def test_timeline_items_strip_dates_and_keep_the_run():
    items = timeline_items(TIMELINE_TEXT)
    assert [i.number for i in items] == [1, 2, 3, 4]
    assert items[0].phrase == "Alpha determinant"
    assert items[1].phrase.startswith("Beta lifts across a long widget chain")
    assert items[2].phrase == "Gamma widget integral of gizmos"
    assert "norm" in items[3].words


def _minimal_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with Helvetica text lines, built by hand (correct xref offsets)."""
    ops = ["BT", "/F1 11 Tf", "72 740 Td"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops += [f"({safe}) Tj", "0 -15 Td"]
    ops.append("ET")
    stream = "\n".join(ops).encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_pdf_text_extraction_feeds_the_timeline(tmp_path):
    pdf = tmp_path / "note.pdf"
    pdf.write_bytes(_minimal_pdf(["1. 10/6/26: Alpha determinant.", "2. 10/9/26: Beta lifts.", "3. 11/1/26: Gamma norm bound."]))
    items = timeline_items(extract_pdf_text(pdf))
    assert [i.phrase for i in items] == ["Alpha determinant", "Beta lifts", "Gamma norm bound"]


def test_match_timeline_uses_vocabulary_overlap_once_per_step():
    specs = partition(synthetic_live())
    vocab = [Counter() for _ in specs]
    target = next(i for i, s in enumerate(specs) if "Weil Bound" in s.title)
    vocab[target].update({"determinant": 50, "weil": 80})
    other = next(i for i, s in enumerate(specs) if "Spectral" in s.title)
    vocab[other].update({"norm": 40, "spectral": 90})
    for bag in vocab[1:]:
        bag.update({"generic": 10})
    items = timeline_items(TIMELINE_TEXT)
    match_timeline(specs, vocab, items)
    assert [i.number for i in specs[target].timeline] == [1]
    assert [i.number for i in specs[other].timeline] == [4]
    assert "determinant" in specs[target].keywords
