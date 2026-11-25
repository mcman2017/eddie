"""The light "ink on gallery paper" palette and the default plot style of the sandbox.

`web/src/palette.json` is the palette's source on the web side. This module mirrors it value
for value for Python (`tests/test_palette_parity.py` holds the two together), and derives the
matplotlib defaults that every sandboxed script gets without asking: the runner points
`MATPLOTLIBRC` at `eddie.matplotlibrc`, so a generated plot script never spends output tokens
on styling and still lands on the page looking like the rest of it.

Stdlib only: the read-only API may import this module, and it never imports matplotlib.
"""

from __future__ import annotations

from pathlib import Path

RC_FILE = Path(__file__).with_name("eddie.matplotlibrc")

SURFACE = {
    "page": "#FAF8F4",
    "well": "#F3EFE6",
    "card": "#FFFFFF",
    "line": "#E2DDD3",
    "lineStrong": "#CFC8BA",
}
INK = {"primary": "#18181B", "secondary": "#44403C", "muted": "#6B645C"}
ACT = {"foundations": "#1D3FBF", "machinery": "#099268", "bottleneck": "#D946EF", "main": "#D9480F"}
CRITICAL_LINE = {"core": "#FFC531", "casing": "#B3122D"}
ZERO = {"ring": "#12B886", "centre": "#F5B301"}
DEAD = {"chalk": "#D9D5CC", "slate": "#8A8F98"}
STATUS = {"critical": "#C81E1E"}

# Height ramps of the 3D strip as (height, colour) stops. Height is softclamp(ln|zeta| / 1.4),
# which runs from -2 (a zero) through 0 (|zeta| = 1, "sea level") to 1; below its first stop a
# ramp keeps that stop's colour.
RAMP_ZETA = (
    (-1.25, "#15207F"),
    (-0.8, "#1F45D6"),
    (-0.45, "#0C93A6"),
    (-0.15, "#22C389"),
    (0.05, "#A9CE48"),
    (0.22, "#F8CC30"),
    (0.4, "#F59A0C"),
    (0.56, "#EC5A3A"),
    (0.7, "#CE3320"),
    (0.86, "#A8187E"),
    (1.0, "#8A1570"),
)
RAMP_WAVE = (
    (-1.6, "#2B2784"),
    (-1.15, "#2C5BCB"),
    (-0.7, "#1295B5"),
    (-0.25, "#27BD86"),
    (0.02, "#A3C13A"),
    (0.28, "#F8B90F"),
    (0.62, "#F26645"),
    (1.0, "#D43A3D"),
)
# Evenly spaced, silence first: paper white up to deep indigo for the loudest.
RAMP_SPECTROGRAM = ("#FFFFFF", "#FDE9B8", "#F9B45C", "#EE6A4D", "#C2267A", "#5B21A6", "#1E1B6E")
# Series colours in drawing order: cobalt, ember, magenta-violet, jade. The order was checked
# for colour-blind separation between neighbours, so keep it.
PLOT_CYCLE = ("#1D3FBF", "#D9480F", "#D946EF", "#099268")

# The whole palette under the keys palette.json uses.
PALETTE = {
    "surface": SURFACE,
    "ink": INK,
    "act": ACT,
    "criticalLine": CRITICAL_LINE,
    "zero": ZERO,
    "dead": DEAD,
    "status": STATUS,
    "rampZeta": RAMP_ZETA,
    "rampWave": RAMP_WAVE,
    "rampSpectrogram": RAMP_SPECTROGRAM,
    "plotCycle": PLOT_CYCLE,
}

# matplotlib defaults for a plot that sits on a white card of the page. Sizes are chosen for a
# figure shown about 540 px wide: 6.4 in at 200 dpi keeps 11 pt text readable after the downscale.
RC: dict[str, object] = {
    "figure.facecolor": SURFACE["card"],
    "axes.facecolor": SURFACE["card"],
    "savefig.facecolor": SURFACE["card"],
    "figure.figsize": (6.4, 3.8),
    "figure.dpi": 200,
    "text.color": INK["primary"],
    "axes.labelcolor": INK["secondary"],
    "xtick.color": INK["muted"],
    "ytick.color": INK["muted"],
    "axes.edgecolor": SURFACE["lineStrong"],
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": SURFACE["line"],
    "grid.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "legend.frameon": False,
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.prop_cycle": PLOT_CYCLE,
    "lines.linewidth": 1.8,
}


def _rc_value(key: str, value: object) -> str:
    """One value in matplotlibrc syntax. `#` starts a comment there, so colours lose theirs."""
    if key == "axes.prop_cycle":
        colours = ", ".join(f"'{c.lstrip('#')}'" for c in value)  # type: ignore[union-attr]
        return f"cycler('color', [{colours}])"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, tuple):
        return ", ".join(str(v) for v in value)
    return str(value).lstrip("#")


def rc_text() -> str:
    """The exact contents of `eddie.matplotlibrc`, rendered from `RC`."""
    head = (
        "# Eddie's default plot style: ink on a white card. Every sandboxed script gets it through\n"
        "# MATPLOTLIBRC. Rendered from eddie/sandbox/style.py (rc_text); do not edit by hand.\n"
        "# A '#' starts a comment in this format, so colours are written without it.\n"
    )
    width = max(len(k) for k in RC) + 1
    return head + "".join(f"{k + ':':<{width}} {_rc_value(k, v)}\n" for k, v in RC.items())
