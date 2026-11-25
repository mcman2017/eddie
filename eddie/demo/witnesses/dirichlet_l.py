"""A Dirichlet L-function along the critical line.

|L(1/2 + it, chi)| for the non-principal character modulo 4 (chi(n) = 0, 1, 0, -1 for
n = 0, 1, 2, 3 mod 4). Its zeros are located by refining each sampled dip with a
golden-section search, and kept only when |L| falls below 1e-3.
"""

import json

import matplotlib
import mpmath
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

P = {"t_max": 30.0, "samples": 900, **globals().get("OVERRIDES", {})}
CHI = [0, 1, 0, -1]

BG, INK, INK2, MUTED, GRID = "#111a2c", "#f0efec", "#c3c2b7", "#898781", "#243049"
SERIES_1, SERIES_2 = "#3987e5", "#d95926"
plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG, "axes.edgecolor": MUTED,
    "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
})


def size(x: float) -> float:
    return float(abs(mpmath.dirichlet(mpmath.mpc(0.5, x), CHI)))


t = np.linspace(0.0, P["t_max"], int(P["samples"]))
mag = np.array([size(x) for x in t])
zeros = []
ratio = (5 ** 0.5 - 1) / 2
for i in range(1, len(t) - 1):
    if not (mag[i] <= mag[i - 1] and mag[i] <= mag[i + 1] and mag[i] < 0.2):
        continue
    a, b = t[i - 1], t[i + 1]
    for _ in range(40):
        c, d = b - ratio * (b - a), a + ratio * (b - a)
        if size(c) < size(d):
            b = d
        else:
            a = c
    x = (a + b) / 2
    if size(x) < 1e-3:
        zeros.append(x)

fig, ax = plt.subplots(figsize=(8, 4.5), dpi=160)
ax.plot(t, mag, color=SERIES_1, lw=1.6, label="|L(1/2 + it, χ)|")
ax.scatter(zeros, np.zeros(len(zeros)), s=28, color=SERIES_2, edgecolor=BG, linewidth=1.2, zorder=3,
           label=f"{len(zeros)} zeros with t < {P['t_max']:g}")
ax.set_xlim(0, P["t_max"])
ax.set_ylim(bottom=0)
ax.set_xlabel("t, the height on the critical line Re(s) = 1/2")
ax.set_ylabel("|L(1/2 + it, χ)|")
ax.set_title("A Dirichlet L-function on the critical line (χ the character mod 4)", loc="left")
ax.legend(loc="upper left")
fig.tight_layout()
fig.savefig("dirichlet_l.png")
plt.close(fig)
with open("dirichlet_l.json", "w") as fh:
    json.dump({"character_mod_4": CHI, "zeros": [round(z, 6) for z in zeros]}, fh, indent=2)
print(f"{len(zeros)} zeros of L(s, chi mod 4) with t < {P['t_max']:g}")
