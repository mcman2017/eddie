"""The critical strip 0 <= Re(s) <= 1, lit by how close zeta(s) comes to zero.

The heat is -log10 |zeta(sigma + it)|, so the zeros glow. Every bright spot in this window
sits on the dashed line Re(s) = 1/2.
"""

import math

import matplotlib
import mpmath
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

P = {"sigma_steps": 41, "t_steps": 196, "t_min": 1.0, "t_max": 40.0, **globals().get("OVERRIDES", {})}

BG, INK, INK2, MUTED = "#111a2c", "#f0efec", "#c3c2b7", "#898781"
plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG, "axes.edgecolor": MUTED,
    "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "font.size": 11,
    "axes.titlesize": 13, "axes.titleweight": "bold",
})
ramp = LinearSegmentedColormap.from_list("eddie_blue", ["#0b1220", "#0d366b", "#1c5cab", "#3987e5", "#86b6ef", "#cde2fb"])

sigma = np.linspace(0.0, 1.0, int(P["sigma_steps"]))
ts = np.linspace(P["t_min"], P["t_max"], int(P["t_steps"]))
heat = np.empty((len(ts), len(sigma)))
for i, t in enumerate(ts):
    for j, s in enumerate(sigma):
        heat[i, j] = -math.log10(max(float(abs(mpmath.zeta(mpmath.mpc(s, t)))), 1e-6))
zeros = []
k = 1
while (gamma := float(mpmath.zetazero(k).imag)) <= P["t_max"]:
    zeros.append(gamma)
    k += 1

fig, ax = plt.subplots(figsize=(6.4, 7.2), dpi=160)
image = ax.imshow(heat, origin="lower", aspect="auto", extent=(0, 1, P["t_min"], P["t_max"]), cmap=ramp,
                  vmin=-1.0, vmax=1.5, interpolation="bicubic")
ax.axvline(0.5, color=INK, lw=0.9, ls=(0, (4, 4)), alpha=0.7)
ax.scatter([0.5] * len(zeros), zeros, s=70, facecolors="none", edgecolors=INK, linewidths=1.0)
ax.set_xlabel("σ = Re(s)")
ax.set_ylabel("t = Im(s)")
ax.set_title("The critical strip: zeros of ζ glow on Re(s) = 1/2", loc="left")
bar = fig.colorbar(image, ax=ax, fraction=0.05, pad=0.03)
bar.set_label("−log₁₀ |ζ(σ + it)|, bright = close to a zero", color=INK2)
bar.outline.set_edgecolor(MUTED)
fig.tight_layout()
fig.savefig("critical_strip.png")
plt.close(fig)
print(f"{len(sigma) * len(ts)} evaluations, {len(zeros)} zeros ringed")
