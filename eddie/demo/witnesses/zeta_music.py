"""The zeros of the Riemann zeta function, played as sound.

Each nontrivial zero 1/2 + i*gamma becomes a sine tone at gamma * hz_per_unit Hz, entering one
after another and weighted by 1/|rho|, the weight each zero carries in Riemann's explicit
formula for the primes. The spectrogram is drawn edge to edge (no axes or margins), so a
player can map time to x exactly; its frequency range is recorded in zeta_zeros.json.
"""

import json
import math
import wave

import matplotlib
import mpmath
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

P = {"zeros": 20, "hz_per_unit": 16.0, "enter_s": 0.3, "hold_s": 2.2, "rate": 22050, "f_max_hz": 1400.0,
     **globals().get("OVERRIDES", {})}

gammas = [float(mpmath.zetazero(k).imag) for k in range(1, int(P["zeros"]) + 1)]
rate = int(P["rate"])
duration = P["enter_s"] * len(gammas) + P["hold_s"]
t = np.arange(int(duration * rate)) / rate
signal = np.zeros_like(t)
for k, gamma in enumerate(gammas):
    start = k * P["enter_s"]
    attack = np.clip((t - start) / 0.04, 0.0, 1.0)
    weight = 1.0 / math.hypot(0.5, gamma)
    signal += weight * attack * np.sin(2 * np.pi * gamma * P["hz_per_unit"] * np.maximum(t - start, 0.0))
signal *= np.clip((duration - t) / 0.6, 0.0, 1.0)  # release
signal *= 0.8 / np.max(np.abs(signal))  # headroom: never clips

with wave.open("zeta_zeros.wav", "wb") as out:
    out.setnchannels(1)
    out.setsampwidth(2)
    out.setframerate(rate)
    out.writeframes((signal * 32767).astype("<i2").tobytes())

# short-time Fourier transform, centered frames so column i sits at time i * hop / rate
frame, hop = 2048, 256
padded = np.pad(signal, frame // 2)
frames = np.lib.stride_tricks.sliding_window_view(padded, frame)[::hop]
spectrum = np.abs(np.fft.rfft(frames * np.hanning(frame), axis=1)).T
freqs = np.fft.rfftfreq(frame, 1 / rate)
keep = freqs <= P["f_max_hz"]
db = 20 * np.log10(spectrum[keep] + 1e-9)

ramp = LinearSegmentedColormap.from_list(
    "eddie_blue", ["#0b1220", "#0d366b", "#1c5cab", "#3987e5", "#86b6ef", "#cde2fb"]
)
fig = plt.figure(figsize=(16, 4), dpi=100)
ax = fig.add_axes((0, 0, 1, 1))
ax.set_axis_off()
ax.imshow(db, origin="lower", aspect="auto", cmap=ramp, vmin=db.max() - 70, vmax=db.max(),
          extent=(0, len(signal) / rate, 0, P["f_max_hz"]), interpolation="bilinear")
fig.savefig("zeta_zeros_spectrogram.png", dpi=100)
plt.close(fig)

with open("zeta_zeros.json", "w") as fh:
    json.dump({"duration_s": round(len(signal) / rate, 4), "f_max_hz": P["f_max_hz"], "hz_per_unit": P["hz_per_unit"],
               "gammas": [round(g, 6) for g in gammas]}, fh, indent=2)
print(f"{len(gammas)} zeros, {len(signal) / rate:.2f} s, tones {gammas[0] * P['hz_per_unit']:.0f} to "
      f"{gammas[-1] * P['hz_per_unit']:.0f} Hz")
