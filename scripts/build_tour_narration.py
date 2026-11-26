"""Render the visual tour's voice-over: `make narration` (VOICE=am_michael by default).

    uv run --group voice python scripts/build_tour_narration.py [--voice am_michael] [--audition]

Kokoro-82M (Apache-2.0 weights; kokoro-onnx, MIT) runs locally on the CPU: $0, no network
once the model files are cached in data/tts/models/ (downloaded once, pinned by sha256).

For each shot in web/src/strip/narration.json, every line is synthesized, trimmed of silence
and placed at its offset; the clip must end `margin_s` before the shot does, or a slightly
faster pace is tried (up to 1.1x) and the build fails if even that overruns. Clips are
loudness-normalized (-16 LUFS, -1.5 dBTP), faded in and out, and encoded as mono 96 kbps MP3
with `-bitexact`, so the same inputs give the same bytes.

Writes web/public/audio/tour/shot_1..5.mp3 and web/public/audio/tour/narration.json (the
manifest the web app reads). `--audition` also renders the whole narration in each candidate
voice into data/tts/audition/ so the voice can be chosen by ear.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "web" / "src" / "strip" / "narration.json"
OUT = ROOT / "web" / "public" / "audio" / "tour"
MODELS = ROOT / "data" / "tts" / "models"
AUDITION = ROOT / "data" / "tts" / "audition"
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
VOICES = ("am_michael", "bm_george", "af_heart")
SPEEDS = (1.0, 1.05, 1.1)
SAMPLE_RATE = 24_000
FADE_S = 0.04
ENCODER_PAD_S = 0.08  # MP3 frames round a clip up by a few tens of milliseconds
SILENCE_DB = -45.0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def ensure_models() -> tuple[Path, Path]:
    MODELS.mkdir(parents=True, exist_ok=True)
    for name, pinned in FILES.items():
        target = MODELS / name
        if not target.exists():
            print(f"downloading {name} ...", flush=True)
            tmp = MODELS / f".{name}.part"
            with urllib.request.urlopen(f"{RELEASE}/{name}", timeout=120) as r, open(tmp, "wb") as fh:  # noqa: S310
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
            os.replace(tmp, target)
        digest = sha256(target)
        if digest != pinned:
            raise SystemExit(f"{name}: sha256 {digest} does not match the pinned {pinned}; delete it and retry")
    return MODELS / "kokoro-v1.0.onnx", MODELS / "voices-v1.0.bin"


def trim(samples: np.ndarray) -> np.ndarray:
    """Cut leading and trailing silence (20 ms windows below SILENCE_DB of the peak)."""
    win = int(0.02 * SAMPLE_RATE)
    peak = float(np.max(np.abs(samples))) or 1.0
    level = peak * 10 ** (SILENCE_DB / 20)
    frames = [np.sqrt(np.mean(samples[i : i + win] ** 2)) for i in range(0, len(samples), win)]
    loud = [i for i, f in enumerate(frames) if f > level]
    if not loud:
        return samples
    return samples[max(0, loud[0] * win - win) : min(len(samples), (loud[-1] + 2) * win)]


def synthesize(kokoro, text: str, voice: str, speed: float) -> np.ndarray:
    samples, rate = kokoro.create(text, voice=voice, speed=speed, lang="en-us")
    if rate != SAMPLE_RATE:
        raise SystemExit(f"unexpected sample rate {rate}")
    return trim(np.asarray(samples, dtype=np.float32))


def assemble(kokoro, shot: dict, voice: str, budget: float) -> tuple[np.ndarray, float, list[dict]]:
    """The shot's clip at the slowest pace that fits its window."""
    for speed in SPEEDS:
        parts, placed, ok = [], [], True
        cursor = 0.0
        for i, line in enumerate(shot["lines"]):
            audio = synthesize(kokoro, line["say"], voice, speed)
            start = max(line["at"], cursor + (0.25 if i else 0.0))
            end = start + len(audio) / SAMPLE_RATE
            nxt = shot["lines"][i + 1]["at"] if i + 1 < len(shot["lines"]) else None
            if nxt is not None and end > nxt - 0.1:
                ok = False  # a line would run into the next caption's line
            placed.append({"at": round(start, 3), "duration_s": round(len(audio) / SAMPLE_RATE, 3), "text": line["text"]})
            parts.append((start, audio))
            cursor = end
        total = cursor
        if ok and total <= budget:
            clip = np.zeros(int(round(total * SAMPLE_RATE)) + 1, dtype=np.float32)
            for start, audio in parts:
                i = int(round(start * SAMPLE_RATE))
                clip[i : i + len(audio)] += audio[: len(clip) - i]
            return clip, speed, placed
        print(f"  {shot['id']}: {total:.2f} s at {speed}x does not fit {budget:.2f} s", flush=True)
    raise SystemExit(f"shot {shot['id']} does not fit its window even at {SPEEDS[-1]}x: shorten its lines")


def write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((pcm * 32767).astype("<i2").tobytes())


def encode(wav: Path, mp3: Path, duration: float) -> None:
    fade_out = max(0.0, duration - FADE_S)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(wav),
        "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d={FADE_S},afade=t=out:st={fade_out:.3f}:d={FADE_S}",
        "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "libmp3lame", "-b:a", "96k", "-map_metadata", "-1",
        "-fflags", "+bitexact", "-flags:a", "+bitexact", "-id3v2_version", "0", str(mp3),
    ], check=True)


def mp3_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return round(float(out.strip()), 3)


def render(kokoro, source: dict, voice: str, out_dir: Path, *, manifest: bool) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    margin = float(source["margin_s"])
    shots = []
    with tempfile.TemporaryDirectory() as tmp:
        for n, shot in enumerate(source["shots"], 1):
            window = round(shot["end"] - shot["start"], 3)
            clip, speed, placed = assemble(kokoro, shot, voice, window - margin - ENCODER_PAD_S)
            wav = Path(tmp) / f"shot_{n}.wav"
            write_wav(wav, clip)
            mp3 = out_dir / f"shot_{n}.mp3"
            encode(wav, mp3, len(clip) / SAMPLE_RATE)
            duration = mp3_duration(mp3)
            if duration > window - margin:
                raise SystemExit(f"shot {n} encodes to {duration} s, over its {window - margin} s budget")
            shots.append({"id": shot["id"], "file": f"/audio/tour/shot_{n}.mp3", "start": shot["start"], "end": shot["end"],
                          "duration_s": duration, "speed": speed, "lines": placed, "bytes": mp3.stat().st_size,
                          "sha256": sha256(mp3)})
            print(f"  shot {n} {shot['id']:<8} {duration:5.2f} s of {window:4.1f} s at {speed}x  {mp3.stat().st_size:,} B")
    if manifest:
        data = {"version": source["version"], "engine": "kokoro-onnx (Kokoro-82M, Apache-2.0)", "voice": voice,
                "models": FILES, "margin_s": margin, "shots": shots}
        (out_dir / "narration.json").write_text(json.dumps(data, indent=2) + "\n")
    return shots


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--voice", default="am_michael", help=f"one of {VOICES} (or any Kokoro voice)")
    ap.add_argument("--audition", action="store_true", help=f"also render every voice in {VOICES} into {AUDITION}")
    args = ap.parse_args(argv)
    from kokoro_onnx import Kokoro

    model, voices = ensure_models()
    kokoro = Kokoro(str(model), str(voices))
    source = json.loads(SOURCE.read_text())
    print(f"narration with {args.voice} -> {OUT}")
    render(kokoro, source, args.voice, OUT, manifest=True)
    if args.audition:
        for voice in VOICES:
            print(f"audition {voice} -> {AUDITION / voice}")
            render(kokoro, source, voice, AUDITION / voice, manifest=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
