"""Hardy's Z(t) on the critical line, drawn twice.

Z(t) is real-valued and |Z(t)| = |zeta(1/2 + it)|, so every sign change is a zero of zeta on
the critical line. hardy_z.png is the clean figure. hardy_z_flawed.png carries deliberate
defects for the R2 critic demo: a clipped y-range, no axis labels, and zero markers hidden
under the curve in the same color. hardy_z.json measures how much of the curve the clipped
range cuts off.
"""

import json

import matplotlib
import mpmath
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

P = {"t_max": 60.0, "samples": 1500, "flawed_ylim": 1.0, **globals().get("OVERRIDES", {})}

BG, INK, INK2, MUTED, GRID = "#111a2c", "#f0efec", "#c3c2b7", "#898781", "#243049"
SERIES_1, SERIES_2 = "#3987e5", "#d95926"
plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG, "axes.edgecolor": MUTED,
    "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
})

t = np.linspace(0.0, P["t_max"], int(P["samples"]))
z = np.array([float(mpmath.siegelz(x)) for x in t])
zeros = []
k = 1
while (gamma := float(mpmath.zetazero(k).imag)) <= P["t_max"]:
    zeros.append(gamma)
    k += 1

fig, ax = plt.subplots(figsize=(8, 4.5), dpi=160)
ax.axhline(0, color=MUTED, lw=0.8)
ax.plot(t, z, color=SERIES_1, lw=1.6, label="Z(t)")
ax.scatter(zeros, np.zeros(len(zeros)), s=28, color=SERIES_2, edgecolor=BG, linewidth=1.2, zorder=3,
           label=f"the {len(zeros)} zeros of ζ(1/2 + it) with t < {P['t_max']:g}")
ax.set_xlim(0, P["t_max"])
ax.set_xlabel("t, the height on the critical line Re(s) = 1/2")
ax.set_ylabel("Z(t)")
ax.set_title("Hardy's Z(t): each sign change is a zero of ζ on the critical line", loc="left")
ax.legend(loc="upper left")
fig.tight_layout()
fig.savefig("hardy_z.png")
plt.close(fig)

lim = float(P["flawed_ylim"])
fig, ax = plt.subplots(figsize=(8, 4.5), dpi=160)
ax.scatter(zeros, np.zeros(len(zeros)), s=28, color=SERIES_1, zorder=1)
ax.plot(t, z, color=SERIES_1, lw=1.6, zorder=2)
ax.set_xlim(0, P["t_max"])
ax.set_ylim(-lim, lim)
ax.set_title("Z(t)", loc="left")
fig.tight_layout()
fig.savefig("hardy_z_flawed.png")
plt.close(fig)

clipped = float(np.mean(np.abs(z) > lim))
with open("hardy_z.json", "w") as fh:
    json.dump({
        "zeros": [round(g, 6) for g in zeros],
        "flawed": {
            "clipped_fraction": round(clipped, 4),
            "planted": ["no axis labels or units", "zero markers hidden under the curve in the same color"],
        },
    }, fh, indent=2)
print(f"{len(zeros)} zeros marked; the flawed y-range cuts off {clipped:.0%} of the curve")
