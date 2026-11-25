"""`python -m eddie.cli seed-demo`: $0 witnesses so the explorer can be clicked end to end.

The witness scripts in eddie/demo/witnesses/ run in the Night 1 sandbox and are attached to
Main's final theorems, matched at run time by the Props their signatures name (no archive
text is stored in git). Every row written here carries source='seed', every text says it is
not an Ember translation, and each run replaces all earlier seed rows and nothing else.
Nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from eddie.sandbox.runner import Limits, SandboxResult, SandboxRunner
from eddie.state.artifacts import ArtifactStore

WITNESS_DIR = Path(__file__).with_name("witnesses")
SEED_PREFIX = "SEED: not an Ember translation."


@dataclass(frozen=True)
class SeedParams:
    zeros: int = 20
    hz_per_unit: float = 16.0
    f_max_hz: float = 1400.0
    hardy_t_max: float = 60.0
    hardy_samples: int = 1500
    strip_sigma_steps: int = 41
    strip_t_steps: int = 196
    strip_t_max: float = 40.0
    l_t_max: float = 30.0
    l_samples: int = 900

    @classmethod
    def quick(cls) -> SeedParams:
        """Small grids for tests: same scripts, seconds instead of tens of seconds."""
        return cls(zeros=4, hardy_t_max=30.0, hardy_samples=240, strip_sigma_steps=9, strip_t_steps=24,
                   strip_t_max=26.0, l_t_max=14.0, l_samples=160)


@dataclass(frozen=True)
class Target:
    decl_id: int
    full_name: str
    signature: str


def _roots(cone_path: Path, index_path: Path) -> list[Target]:
    con = sqlite3.connect(f"file:{cone_path}?mode=ro", uri=True)
    try:
        con.execute("ATTACH ? AS idx", (f"file:{index_path}?mode=ro",))
        rows = con.execute(
            "SELECT d.id, d.full_name, d.signature FROM decl_metrics m JOIN idx.decls d ON d.id = m.decl_id "
            "WHERE m.is_root = 1 AND d.full_name IS NOT NULL ORDER BY d.id"
        ).fetchall()
    finally:
        con.close()
    return [Target(int(r[0]), r[1], r[2] or "") for r in rows]


def pick_targets(roots: list[Target]) -> dict[str, Target]:
    """Main theorem for the zeta witnesses, then the zeta-zeros and Dirichlet-zeros statements."""
    if not roots:
        raise ValueError("the cone has no root theorems: run `python -m eddie.cli cone build` first")
    used: set[int] = set()

    def take(pred: Callable[[Target], bool]) -> Target:
        for r in roots:
            if r.decl_id not in used and pred(r):
                used.add(r.decl_id)
                return r
        for r in roots:  # fall back to the next unused root, then to the first one
            if r.decl_id not in used:
                used.add(r.decl_id)
                return r
        return roots[0]

    rh = take(lambda r: "RiemannHypothesis" in r.signature)
    strip = take(lambda r: "Zeta" in r.signature and "Zeros" in r.signature)
    lfun = take(lambda r: "Dirichlet" in r.signature and "Zeros" in r.signature)
    return {"rh": rh, "strip": strip, "lfun": lfun}


def _script(name: str, overrides: dict[str, Any]) -> str:
    return f"OVERRIDES = {overrides!r}\n\n" + (WITNESS_DIR / name).read_text(encoding="utf-8")


def _artifact(result: SandboxResult, name: str) -> Any:
    for a in result.artifacts:
        if a.name == name:
            return a
    raise RuntimeError(f"sandbox job {result.job_id} produced no {name} (status {result.status}): {result.stderr[-400:]}")


def _json_artifact(result: SandboxResult, name: str) -> dict[str, Any]:
    return json.loads(Path(_artifact(result, name).store_path).read_text())


INTUITIONS = {
    "rh": (
        "The Riemann Hypothesis: every nontrivial zero of the zeta function ζ(s) lies on the critical line, "
        "where the real part of s is exactly 1/2.\n\n"
        "The witnesses make that concrete. On the critical line Hardy's function Z(t) is real, so each zero "
        "shows up as a sign change. Played as sound, each zero becomes a tone whose pitch is its height on "
        "the line, weighted by 1/|ρ| as in Riemann's explicit formula for the primes."
    ),
    "strip": (
        "In plain terms: the zeros of ζ(s) inside the critical strip all sit on the line Re(s) = 1/2. The "
        "precise hypotheses are in the formal statement below.\n\n"
        "The heatmap lights up wherever |ζ(s)| gets close to zero; in this window every bright spot lies on the "
        "dashed line."
    ),
    "lfun": (
        "The same claim for Dirichlet L-functions L(s, χ): their nontrivial zeros lie on Re(s) = 1/2. The "
        "precise hypotheses are in the formal statement below.\n\n"
        "The plot follows |L(1/2 + it, χ)| for the character modulo 4 and marks each place it vanishes."
    ),
}


def seed_demo(
    *,
    cone_path: Path,
    index_path: Path,
    db_path: Path,
    artifact_dir: Path,
    params: SeedParams | None = None,
    activity: bool = True,
    tmp_root: Path | None = None,
    mpl_cache: Path | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    p = params or SeedParams()
    targets = pick_targets(_roots(cone_path, index_path))
    runner = SandboxRunner(artifact_dir, limits=Limits(wall_s=120, cpu_s=120), tmp_root=tmp_root, mpl_cache=mpl_cache)
    scripts = {
        "music": _script("zeta_music.py", {"zeros": p.zeros, "hz_per_unit": p.hz_per_unit, "f_max_hz": p.f_max_hz}),
        "hardy": _script("hardy_z.py", {"t_max": p.hardy_t_max, "samples": p.hardy_samples}),
        "strip": _script("critical_strip.py", {"sigma_steps": p.strip_sigma_steps, "t_steps": p.strip_t_steps,
                                               "t_max": p.strip_t_max}),
        "lfun": _script("dirichlet_l.py", {"t_max": p.l_t_max, "samples": p.l_samples}),
    }

    async def run_all() -> dict[str, SandboxResult]:
        results = await asyncio.gather(*(runner.run(s) for s in scripts.values()))
        return dict(zip(scripts, results, strict=True))

    results = asyncio.run(run_all())
    for key, res in results.items():
        log(f"sandbox  {key:<6} {res.status:<7} {res.duration_s:>6.2f}s  {res.stdout.strip()[:120]}")
        if res.status != "ok":
            raise RuntimeError(f"witness script {key} failed ({res.status}): {res.stderr[-600:]}")

    store = ArtifactStore(db_path)
    store.clear_source("seed")
    written: list[dict[str, Any]] = []

    def witness(target: Target, res: SandboxResult, name: str, *, kind: str, status: str, title: str, script: str,
                **extra: Any) -> int:
        art = _artifact(res, name)
        qc = extra.pop("qc", None) or res.qc.get(name)
        wid = store.add_witness(
            target.full_name, kind=kind, artifact_sha256=art.sha256, ext=Path(name).suffix.lstrip("."),
            source="seed", status=status, title=title, decl_id=target.decl_id, bytes_=art.bytes, python_source=script,
            sandbox_job_id=res.job_id, qc=qc, **extra,
        )
        written.append({"id": wid, "decl": target.full_name, "kind": kind, "status": status, "sha256": art.sha256,
                        "ext": Path(name).suffix.lstrip(".")})
        return wid

    rh, strip, lfun = targets["rh"], targets["strip"], targets["lfun"]

    music = results["music"]
    meta = _json_artifact(music, "zeta_zeros.json")
    audio_qc = music.qc.get("zeta_zeros.wav", {})
    audio_id = witness(rh, music, "zeta_zeros.wav", kind="audio", status="verified",
                       title=f"The first {len(meta['gammas'])} zeta zeros, sounded one by one",
                       script=scripts["music"], defects=audio_qc.get("defects", []))
    witness(rh, music, "zeta_zeros_spectrogram.png", kind="spectrogram", status="verified",
            title="Spectrogram: each zero enters as a new line", script=scripts["music"], pair_id=audio_id,
            qc={"f_max_hz": meta["f_max_hz"], "duration_s": meta["duration_s"], "gammas": meta["gammas"]})

    hardy = results["hardy"]
    hmeta = _json_artifact(hardy, "hardy_z.json")
    flawed = hmeta["flawed"]
    defects = [f"y-range clipped: {flawed['clipped_fraction']:.0%} of the curve is cut off (measured)"]
    defects += [f"{d} (planted)" for d in flawed["planted"]]
    bad = witness(rh, hardy, "hardy_z_flawed.png", kind="plot", status="rejected",
                  title="Hardy's Z(t), first render", script=scripts["hardy"], defects=defects)
    good = witness(rh, hardy, "hardy_z.png", kind="plot", status="verified",
                   title="Hardy's Z(t) on the critical line", script=scripts["hardy"], supersedes_id=bad, pair_id=bad)
    store.add_review(region="R2.critic", verdict="rejected", source="seed", witness_id=bad, decl_full_name=rh.full_name,
                     reasons=["SEED review: the defects above were planted to exercise the R2 critic"])
    store.add_review(region="R2.critic", verdict="verified", source="seed", witness_id=good, decl_full_name=rh.full_name,
                     reasons=[f"SEED review: full y-range, labeled axes, {len(hmeta['zeros'])} zeros marked"])

    witness(strip, results["strip"], "critical_strip.png", kind="plot", status="verified",
            title="The critical strip, lit by −log|ζ|", script=scripts["strip"])
    lmeta = _json_artifact(results["lfun"], "dirichlet_l.json")
    witness(lfun, results["lfun"], "dirichlet_l.png", kind="plot", status="verified",
            title=f"|L(1/2 + it, χ mod 4)| with {len(lmeta['zeros'])} zeros", script=scripts["lfun"])

    for key, target in (("rh", rh), ("strip", strip), ("lfun", lfun)):
        store.put_intuition(target.full_name, f"{SEED_PREFIX} {INTUITIONS[key]}", source="seed", decl_id=target.decl_id,
                            formal_signature=target.signature)
    if activity:
        store.set_activity("R2.critic", decl_full_name=rh.full_name, message="SEED demo: reviewing a witness render",
                           source="seed")
    return {
        "targets": {k: asdict(v) for k, v in targets.items()},
        "witnesses": written,
        "sandbox": {k: {"status": r.status, "duration_s": r.duration_s, "job_id": r.job_id} for k, r in results.items()},
    }
