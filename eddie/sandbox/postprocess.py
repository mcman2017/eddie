"""Trusted post-pass, run inside the same sandbox guards after a generated script finishes.

For every `.wav` it renders a spectrogram PNG. For every image and audio file it computes
$0 QC metrics and defects, written to `qc.json`. This lets Region 2's paid multimodal
critic skip artifacts that are blank, NaN-filled, silent, clipped or too short.
"""

from __future__ import annotations

import json
import os
import sys
import wave

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import image as mpimg  # noqa: E402

INTERNAL = {"main.py", "qc.json", ".sandbox_violations"}
SKIP_DIRS = {"tmp", ".mpl", "__pycache__"}


def _samples(path: str) -> tuple[np.ndarray, int, int]:
    with wave.open(path, "rb") as w:
        channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(min(frames, rate * 180))
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float64) / float(1 << 23)
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
    else:
        raise ValueError(f"unsupported sample width {width}")
    if channels > 1:
        x = x[: len(x) - len(x) % channels].reshape(-1, channels).mean(axis=1)
    return x, rate, frames


def analyze_wav(path: str) -> dict:
    x, rate, frames = _samples(path)
    duration = frames / rate if rate else 0.0
    finite = np.isfinite(x)
    rms = float(np.sqrt(np.mean(x[finite] ** 2))) if finite.any() else 0.0
    peak = float(np.max(np.abs(x[finite]))) if finite.any() else 0.0
    clipped = float(np.mean(np.abs(x) >= 0.999)) if len(x) else 0.0
    defects = []
    if not finite.all():
        defects.append("nan_or_inf")
    if rms < 1e-4:
        defects.append("silent")
    if clipped > 0.01:
        defects.append("clipped")
    if duration < 0.25:
        defects.append("too_short")
    spectrogram = None
    if len(x) >= 1024 and finite.all():
        frame, hop = 1024, 256
        window = np.hanning(frame)
        n_frames = 1 + (len(x) - frame) // hop
        frames_ = np.lib.stride_tricks.as_strided(x, shape=(n_frames, frame), strides=(x.strides[0] * hop, x.strides[0]))
        mag = np.abs(np.fft.rfft(frames_ * window, axis=1)).T
        db = 20 * np.log10(mag + 1e-9)
        fig, ax = plt.subplots(figsize=(8, 3), dpi=100)
        ax.imshow(db, origin="lower", aspect="auto", cmap="magma",
                  extent=[0, len(x) / rate, 0, rate / 2], vmin=db.max() - 90, vmax=db.max())
        ax.set_xlabel("time (s)")
        ax.set_ylabel("frequency (Hz)")
        ax.set_title(os.path.basename(path))
        fig.tight_layout()
        spectrogram = path[: -len(".wav")] + ".spectrogram.png"
        fig.savefig(spectrogram)
        plt.close(fig)
    return {
        "kind": "audio",
        "sample_rate": rate,
        "duration_s": round(duration, 4),
        "rms": round(rms, 6),
        "peak": round(peak, 6),
        "clipped_fraction": round(clipped, 6),
        "defects": defects,
        "spectrogram": os.path.basename(spectrogram) if spectrogram else None,
    }


def analyze_png(path: str) -> dict:
    img = np.asarray(mpimg.imread(path), dtype=np.float64)
    h, w = img.shape[:2]
    rgb = img[..., :3] if img.ndim == 3 else img
    lum = rgb.mean(axis=-1) if rgb.ndim == 3 else rgb
    defects = []
    if not np.isfinite(img).all():
        defects.append("nan_or_inf")
    std = float(np.nanstd(lum))
    if std < 1e-3:
        defects.append("blank_or_constant")
    if min(h, w) < 32:
        defects.append("tiny")
    return {"kind": "image", "width": w, "height": h, "luminance_std": round(std, 6), "defects": defects}


def main(job_dir: str) -> None:
    report: dict[str, dict] = {}
    for root, dirs, files in os.walk(job_dir, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, job_dir)
            if rel in INTERNAL or not os.path.isfile(path) or os.path.islink(path):
                continue
            try:
                if name.lower().endswith(".wav"):
                    report[rel] = analyze_wav(path)
                elif name.lower().endswith(".png") and not name.endswith(".spectrogram.png"):
                    report[rel] = analyze_png(path)
            except Exception as exc:  # noqa: BLE001 - QC must never crash the job
                report[rel] = {"kind": "unknown", "defects": ["unreadable"], "error": f"{type(exc).__name__}: {exc}"}
    with open(os.path.join(job_dir, "qc.json"), "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)


if __name__ == "__main__":
    main(sys.argv[1])
