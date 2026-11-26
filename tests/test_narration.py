"""The tour's voice-over stays in step with the tour.

web/src/strip/narration.json (the lines) must use the shot windows of CUES in tour.ts, and the
rendered manifest web/public/audio/tour/narration.json must describe the committed MP3s exactly:
same files, same hashes, same text, every clip ending inside its window. Re-timing the tour
without re-rendering the narration fails here.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "web" / "src" / "strip" / "narration.json"
MANIFEST = ROOT / "web" / "public" / "audio" / "tour" / "narration.json"
TOUR = ROOT / "web" / "src" / "strip" / "tour.ts"


def tour_windows() -> list[tuple[str, float, float]]:
    """(shot, start, end) from tour.ts: a shot starts at its first cue and ends where the next shot starts."""
    text = TOUR.read_text()
    total = float(re.search(r"export const TOUR_SECONDS = ([0-9.]+);", text).group(1))
    cues = re.findall(r'\{\s*shot:\s*"(\w+)",\s*from:\s*([0-9.]+)', text)
    starts: list[tuple[str, float]] = []
    for shot, start in cues:
        if not starts or starts[-1][0] != shot:
            starts.append((shot, float(start)))
    return [(shot, start, starts[i + 1][1] if i + 1 < len(starts) else total) for i, (shot, start) in enumerate(starts)]


def test_the_lines_use_the_tours_own_shot_windows():
    source = json.loads(SOURCE.read_text())
    windows = tour_windows()
    assert len(windows) == 5
    assert [(s["id"], s["start"], s["end"]) for s in source["shots"]] == windows


def test_the_manifest_describes_the_committed_clips_exactly():
    source = json.loads(SOURCE.read_text())
    manifest = json.loads(MANIFEST.read_text())
    assert [s["id"] for s in manifest["shots"]] == [s["id"] for s in source["shots"]]
    margin = manifest["margin_s"]
    total = 0
    for n, (clip, shot) in enumerate(zip(manifest["shots"], source["shots"], strict=True), 1):
        path = ROOT / "web" / "public" / clip["file"].lstrip("/")
        assert clip["file"] == f"/audio/tour/shot_{n}.mp3" and path.is_file()
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == clip["sha256"] and len(data) == clip["bytes"]
        assert data[:3] == b"ID3" or data[0] == 0xFF  # an MP3 frame or tag
        assert (clip["start"], clip["end"]) == (shot["start"], shot["end"])
        assert 0 < clip["duration_s"] <= clip["end"] - clip["start"] - margin, clip["id"]
        assert [line["text"] for line in clip["lines"]] == [line["text"] for line in shot["lines"]]
        assert 0.95 <= clip["speed"] <= 1.1
        total += len(data)
    assert total < 1_000_000  # five short clips, well under a megabyte


def test_the_words_follow_the_findings_rules():
    words = " ".join(line["text"] + " " + line["say"] for s in json.loads(SOURCE.read_text())["shots"] for line in s["lines"])
    assert "emerald" in words  # the zero markers are emerald rings (palette.json zero.ring), not blue
    for banned in ("bottleneck", "stable", "proves", "page"):
        assert banned not in words.lower(), banned
