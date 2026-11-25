"""The critical strip 0 <= Re(s) <= 1 seen from above, painted like the 3D relief model.

The colour is the height of that model: h = softclamp(ln|zeta(sigma + it)| / 1.4), which is 0
("sea level") where |zeta| = 1, falls to -2 at a zero and rises toward 1 where zeta is large.
The same colour ramp is used, so a deep blue well here is a well there. Every well in this
window sits on the line Re(s) = 1/2. Paper, ink and fonts come from the sandbox's default
plot style.
"""

import matplotlib
import mpmath
import numpy as np

matplotlib.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

P = {"sigma_steps": 41, "t_steps": 196, "t_min": 1.0, "t_max": 40.0, **globals().get("OVERRIDES", {})}

# (height, colour) stops of the zeta ramp; below the first stop the colour stays the same
RAMP = [(-1.25, "#15207F"), (-0.8, "#1F45D6"), (-0.45, "#0C93A6"), (-0.15, "#22C389"), (0.05, "#A9CE48"),
        (0.22, "#F8CC30"), (0.4, "#F59A0C"), (0.56, "#EC5A3A"), (0.7, "#CE3320"), (0.86, "#A8187E"), (1.0, "#8A1570")]
LO, HI = -2.0, 1.0
GOLD, CRIMSON = "#FFC531", "#B3122D"  # the critical line: a gold core over a crimson casing
RING, CENTRE, CARD = "#12B886", "#F5B301", "#FFFFFF"  # a zero: an emerald ring around a gold centre


def height(mag):
    u = np.log(np.maximum(mag, 1e-12)) / 1.4
    return np.where(u >= 0, np.tanh(u), -2 * np.tanh(-u / 2))


sigma = np.linspace(0.0, 1.0, int(P["sigma_steps"]))
ts = np.linspace(P["t_min"], P["t_max"], int(P["t_steps"]))
mag = np.array([[float(abs(mpmath.zeta(mpmath.mpc(s, t)))) for s in sigma] for t in ts])
zeros = []
k = 1
while (gamma := float(mpmath.zetazero(k).imag)) <= P["t_max"]:
    zeros.append(gamma)
    k += 1

ramp = LinearSegmentedColormap.from_list(
    "eddie_zeta", [(0.0, RAMP[0][1])] + [((h - LO) / (HI - LO), colour) for h, colour in RAMP]
)
fig, ax = plt.subplots(figsize=(6.4, 7.0), layout="constrained")
image = ax.imshow(height(mag), origin="lower", aspect="auto", extent=(0, 1, P["t_min"], P["t_max"]), cmap=ramp,
                  vmin=LO, vmax=HI, interpolation="bicubic")
ax.grid(False)
ax.axvline(0.5, color=GOLD, lw=2.2, path_effects=[pe.Stroke(linewidth=5.2, foreground=CRIMSON), pe.Normal()],
           label="the critical line Re(s) = 1/2")
ax.scatter([0.5] * len(zeros), zeros, s=250, color=CARD, linewidths=0, zorder=3)
ax.scatter([0.5] * len(zeros), zeros, s=110, color=CENTRE, edgecolors=RING, linewidths=2.6, zorder=4,
           label=f"zero of ζ ({len(zeros)} with t < {P['t_max']:g})")
for n, gamma in enumerate(zeros, start=1):
    ax.annotate(f"zero {n}: t = {gamma:.2f}", (0.5, gamma), xytext=(13, 0), textcoords="offset points", va="center",
                fontsize=9, bbox={"boxstyle": "round,pad=0.3", "facecolor": CARD, "edgecolor": "none", "alpha": 0.92})
ax.set_xlabel("σ = Re(s)")
ax.set_ylabel("t = Im(s)")
ax.set_title("The critical strip: every well sits on Re(s) = 1/2")
sizes = [0.01, 0.1, 0.3, 1, 3, 10]
bar = fig.colorbar(image, ax=ax, fraction=0.05, pad=0.03, ticks=[float(height(x)) for x in sizes])
bar.ax.set_yticklabels([f"{x:g}" for x in sizes])
bar.set_label("|ζ(σ + it)|: the blue wells are zeros, green is |ζ| = 1")
bar.outline.set_visible(False)
fig.legend(loc="outside lower center", ncols=2, fontsize=10)
fig.savefig("critical_strip.png")
plt.close(fig)
print(f"{len(sigma) * len(ts)} evaluations, {len(zeros)} zeros ringed")
