"""Hardy's Z(t) on the critical line, drawn twice.

Z(t) is real-valued and |Z(t)| = |zeta(1/2 + it)|, so every sign change is a zero of zeta on
the critical line. hardy_z.png is the clean figure: the lobes above and below zero are tinted
in two colours, so each zero shows as a colour flip. hardy_z_flawed.png carries deliberate
defects for the R2 critic demo: a clipped y-range, no axis labels, and zero markers hidden
under the curve in the same color. hardy_z.json measures how much of the curve the clipped
range cuts off. Paper, ink, grid and fonts come from the sandbox's default plot style.
"""

import json

import matplotlib
import mpmath
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

P = {"t_max": 60.0, "samples": 1500, "flawed_ylim": 1.0, **globals().get("OVERRIDES", {})}

COBALT, EMBER, MAGENTA, JADE = "#1D3FBF", "#D9480F", "#D946EF", "#099268"
STONE, CARD = "#6B645C", "#FFFFFF"

t = np.linspace(0.0, P["t_max"], int(P["samples"]))
z = np.array([float(mpmath.siegelz(x)) for x in t])
zeros = []
k = 1
while (gamma := float(mpmath.zetazero(k).imag)) <= P["t_max"]:
    zeros.append(gamma)
    k += 1

fig, ax = plt.subplots(layout="constrained")
ax.axhline(0, color=STONE, lw=1.0)
ax.plot(t, z, color=COBALT, label="Z(t)", zorder=2)
ax.fill_between(t, z, 0, where=z > 0, interpolate=True, color=JADE, alpha=0.3, lw=0, label="Z(t) > 0")
ax.fill_between(t, z, 0, where=z < 0, interpolate=True, color=MAGENTA, alpha=0.3, lw=0, label="Z(t) < 0")
ax.scatter(zeros, np.zeros(len(zeros)), s=52, color=EMBER, edgecolor=CARD, linewidth=1.3, zorder=3,
           label=f"zero of ζ ({len(zeros)} with t < {P['t_max']:g})")
ax.set_xlim(0, P["t_max"])
ax.set_xlabel("t, the height on the critical line Re(s) = 1/2")
ax.set_ylabel("Z(t)")
ax.set_title("Hardy's Z(t): every sign change is a zero of ζ")
fig.legend(loc="outside lower center", ncols=4, fontsize=10, columnspacing=1.4, handletextpad=0.5)
fig.savefig("hardy_z.png")
plt.close(fig)

lim = float(P["flawed_ylim"])
fig, ax = plt.subplots(layout="constrained")
ax.scatter(zeros, np.zeros(len(zeros)), s=28, color=COBALT, zorder=1)
ax.plot(t, z, color=COBALT, zorder=2)
ax.set_xlim(0, P["t_max"])
ax.set_ylim(-lim, lim)
ax.set_title("Z(t)")
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
