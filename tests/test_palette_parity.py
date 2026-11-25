"""One palette, three places: web/src/palette.json, eddie/sandbox/style.py, and the plot scripts.

The web UI reads palette.json. The sandbox's default plot style and the witness scripts carry
the same colours as Python constants and literals (the scripts are shown in the UI as written,
so they cannot import them). These tests fail when any copy drifts.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import matplotlib
import pytest
from matplotlib.colors import to_hex

from eddie.sandbox import style

ROOT = Path(__file__).resolve().parent.parent
PALETTE_JSON = json.loads((ROOT / "web" / "src" / "palette.json").read_text(encoding="utf-8"))
WITNESSES = sorted((ROOT / "eddie" / "demo" / "witnesses").glob("*.py"))
POSTPROCESS = ROOT / "eddie" / "sandbox" / "postprocess.py"
HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")


def _plain(value):
    """Tuples as lists, so Python constants compare equal to parsed JSON."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _colours(value) -> set[str]:
    if isinstance(value, str):
        return {value.upper()} if HEX.fullmatch(value) else set()
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return set().union(*map(_colours, value))
    return set()


PALETTE_COLOURS = _colours(PALETTE_JSON)


def _assigned(path: Path, name: str):
    """The literal assigned to a module-level name, read without running the script."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} assigns no {name}")


def test_the_json_palette_has_the_expected_sections():
    assert set(PALETTE_JSON) == {"surface", "ink", "act", "criticalLine", "zero", "dead", "status", "rampZeta",
                                 "rampWave", "rampSpectrogram", "plotCycle"}
    assert len(PALETTE_COLOURS) > 40


@pytest.mark.parametrize("key", sorted(PALETTE_JSON))
def test_style_py_matches_palette_json_value_for_value(key):
    assert _plain(style.PALETTE[key]) == PALETTE_JSON[key]


def test_the_named_constants_are_the_palette_sections():
    assert set(style.PALETTE) == set(PALETTE_JSON)
    named = {
        "surface": style.SURFACE, "ink": style.INK, "act": style.ACT, "criticalLine": style.CRITICAL_LINE,
        "zero": style.ZERO, "dead": style.DEAD, "status": style.STATUS, "rampZeta": style.RAMP_ZETA,
        "rampWave": style.RAMP_WAVE, "rampSpectrogram": style.RAMP_SPECTROGRAM, "plotCycle": style.PLOT_CYCLE,
    }
    assert all(style.PALETTE[k] is v for k, v in named.items())
    # cobalt, ember, magenta-violet, jade: the order checked for colour-blind separation
    assert list(style.PLOT_CYCLE) == ["#1D3FBF", "#D9480F", "#D946EF", "#099268"]
    for ramp in (style.RAMP_ZETA, style.RAMP_WAVE):
        heights = [h for h, _ in ramp]
        assert heights == sorted(heights) and -2.0 <= heights[0] and heights[-1] == 1.0


def test_the_packaged_rc_file_is_rendered_from_style_py():
    assert style.RC_FILE.name == "eddie.matplotlibrc" and style.RC_FILE.parent == ROOT / "eddie" / "sandbox"
    assert style.RC_FILE.read_text(encoding="utf-8") == style.rc_text()
    # a '#' would start a comment and silently drop the value
    settings = [line for line in style.rc_text().splitlines() if line and not line.startswith("#")]
    assert len(settings) == len(style.RC) and not any("#" in line for line in settings)


def test_the_rc_file_parses_to_the_palette():
    rc = matplotlib.rc_params_from_file(str(style.RC_FILE), fail_on_error=True, use_default_template=False)
    assert set(rc) == set(style.RC)
    expected = {
        "figure.facecolor": PALETTE_JSON["surface"]["card"],
        "axes.facecolor": PALETTE_JSON["surface"]["card"],
        "savefig.facecolor": PALETTE_JSON["surface"]["card"],
        "text.color": PALETTE_JSON["ink"]["primary"],
        "axes.labelcolor": PALETTE_JSON["ink"]["secondary"],
        "xtick.color": PALETTE_JSON["ink"]["muted"],
        "ytick.color": PALETTE_JSON["ink"]["muted"],
        "axes.edgecolor": PALETTE_JSON["surface"]["lineStrong"],
        "grid.color": PALETTE_JSON["surface"]["line"],
    }
    colour_keys = {k for k in rc if k.endswith("color")}
    assert colour_keys == set(expected)  # every colour the file sets is checked below
    for key, colour in expected.items():
        assert to_hex(rc[key]).upper() == colour, key
    assert expected["figure.facecolor"] == "#FFFFFF"  # the plots sit on white cards
    assert [to_hex(c).upper() for c in rc["axes.prop_cycle"].by_key()["color"]] == PALETTE_JSON["plotCycle"]
    assert rc["axes.grid"] is True and rc["grid.linewidth"] == 0.6
    assert rc["axes.spines.top"] is False and rc["axes.spines.right"] is False and rc["legend.frameon"] is False
    assert (rc["font.size"], rc["axes.titlesize"], rc["axes.titleweight"]) == (11, 13, "bold")
    assert 1.6 <= rc["lines.linewidth"] <= 2.0


@pytest.mark.parametrize("path", [*WITNESSES, POSTPROCESS], ids=lambda p: p.name)
def test_every_colour_literal_in_a_plot_script_is_a_palette_colour(path):
    literals = HEX.findall(path.read_text(encoding="utf-8"))
    if path.name == "__init__.py":
        assert not literals
        return
    assert literals, f"{path.name} names no colour at all"
    strays = sorted({c for c in literals if c.upper() not in PALETTE_COLOURS})
    assert not strays, f"{path.name} uses colours outside palette.json: {strays}"


def test_witness_scripts_are_self_contained_and_leave_styling_to_the_rc_file():
    scripts = [p for p in WITNESSES if p.name != "__init__.py"]
    assert [p.name for p in scripts] == ["critical_strip.py", "dirichlet_l.py", "hardy_z.py", "zeta_music.py"]
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert "rcParams" not in text and "plt.style" not in text, path.name
        assert not re.search(r"^\s*(?:from|import)\s+eddie\b", text, re.M), path.name


def test_the_ramps_written_into_scripts_are_the_palette_ramps():
    witnesses = ROOT / "eddie" / "demo" / "witnesses"
    assert _plain(_assigned(witnesses / "critical_strip.py", "RAMP")) == PALETTE_JSON["rampZeta"]
    assert _assigned(POSTPROCESS, "SPECTROGRAM_RAMP") == PALETTE_JSON["rampSpectrogram"]
    assert HEX.findall((witnesses / "zeta_music.py").read_text(encoding="utf-8")) == PALETTE_JSON["rampSpectrogram"]
    strip = (witnesses / "critical_strip.py").read_text(encoding="utf-8")
    for colour in (*PALETTE_JSON["criticalLine"].values(), *PALETTE_JSON["zero"].values()):
        assert colour in strip, colour
