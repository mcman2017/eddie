"""Sandboxed runner: artifacts + spectrograms + QC, timeouts, and every guard."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest
from matplotlib import image as mpimg

from eddie.sandbox.artifacts import sweep_stale
from eddie.sandbox.runner import Limits, SandboxRunner
from eddie.sandbox.style import PLOT_CYCLE, RC_FILE

pytestmark = [pytest.mark.anyio, pytest.mark.slow]


@pytest.fixture(scope="session")
def mpl_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("mplcache")


@pytest.fixture
def runner(tmp_path, mpl_cache):
    return SandboxRunner(
        tmp_path / "artifacts",
        limits=Limits(wall_s=20, cpu_s=20),
        tmp_root=tmp_path / "jobs",
        mpl_cache=mpl_cache,
    )


GOOD = textwrap.dedent(
    """
    import wave
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpmath import mp, zetazero

    mp.dps = 15
    gammas = [float(zetazero(k).imag) for k in range(1, 6)]
    t = np.linspace(0, 2, 2 * 8000, endpoint=False)
    signal = sum(np.sin(2 * np.pi * (110 + 10 * g) * t) for g in gammas) / len(gammas)
    with wave.open("zeros.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes((signal * 0.8 * 32767).astype("<i2").tobytes())
    fig, ax = plt.subplots()
    ax.plot(range(1, 6), gammas, "o-"); ax.set_xlabel("k"); ax.set_ylabel("Im ρ_k")
    fig.savefig("gammas.png")
    print("first zero", round(gammas[0], 6))
    """
)


async def test_plot_and_wav_produce_artifacts_spectrogram_and_clean_qc(runner, tmp_path):
    res = await runner.run(GOOD)
    assert res.status == "ok", (res.stderr, res.violations)
    assert "first zero 14.134725" in res.stdout
    names = {a.name for a in res.artifacts}
    assert {"zeros.wav", "gammas.png", "zeros.spectrogram.png", "qc.json"} <= names
    assert res.qc["zeros.wav"]["defects"] == []
    assert res.qc["gammas.png"]["defects"] == []
    assert res.qc["zeros.wav"]["duration_s"] == 2.0
    # the automatic spectrogram is drawn for the light page: white paper, and silence stays white
    spectrogram = next(a for a in res.artifacts if a.name == "zeros.spectrogram.png")
    pixels = mpimg.imread(spectrogram.store_path)[..., :3]
    assert pixels[0].min() == 1.0 and pixels[:, 0].min() == 1.0
    luminance = pixels.mean(axis=-1)
    assert (luminance > 0.9).mean() > 0.5 and luminance.min() < 0.35
    stored = [(a.store_path, a.sha256 + os.path.splitext(a.name)[1]) for a in res.artifacts]
    assert all(path.endswith(suffix) for path, suffix in stored)
    assert all(map(os.path.exists, (p for p, _ in stored)))  # noqa: ASYNC240
    assert list((tmp_path / "jobs").iterdir()) == []  # job dir cleaned up


async def test_defective_artifacts_are_flagged(runner):
    script = textwrap.dedent(
        """
        import wave
        import numpy as np
        import matplotlib.pyplot as plt
        with wave.open("silent.wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(np.zeros(8000, dtype="<i2").tobytes())
        with wave.open("clipped.wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(np.full(8000, 32767, dtype="<i2").tobytes())
        plt.imsave("blank.png", np.ones((64, 64, 3)))
        """
    )
    res = await runner.run(script)
    assert res.status == "ok", res.stderr
    assert "silent" in res.defects["silent.wav"]
    assert "clipped" in res.defects["clipped.wav"]
    assert "blank_or_constant" in res.defects["blank.png"]


async def test_infinite_loop_is_killed_at_the_wall_clock_timeout(runner):
    res = await runner.run("while True:\n    pass\n", timeout_s=2, postprocess=False)
    assert res.status == "timeout"
    assert res.duration_s < 6


@pytest.mark.parametrize(
    "snippet,event",
    [
        ("import socket\nsocket.create_connection(('192.0.2.1', 443), timeout=1)", "socket"),
        ("import subprocess\nsubprocess.run(['ls'])", "subprocess.Popen"),
        ("import os\nos.system('true')", "os.system"),
    ],
)
async def test_network_and_process_creation_are_denied(runner, snippet, event):
    res = await runner.run(snippet, postprocess=False)
    assert res.status == "policy_violation"
    assert any(event in v["event"] for v in res.violations), res.violations


async def test_writes_outside_the_job_dir_are_denied(runner, tmp_path):
    target = tmp_path / "escape.txt"
    res = await runner.run(f"open({str(target)!r}, 'w').write('x')", postprocess=False)
    assert res.status == "policy_violation"
    assert not target.exists()


async def test_a_script_that_sets_no_style_gets_the_light_house_style(runner):
    script = textwrap.dedent(
        """
        import matplotlib
        import matplotlib.pyplot as plt
        rc = matplotlib.rcParams
        print("RCFILE", matplotlib.matplotlib_fname())
        print("PAPER", rc["figure.facecolor"], rc["axes.facecolor"], rc["savefig.facecolor"])
        print("INK", rc["text.color"], rc["axes.labelcolor"], rc["xtick.color"])
        print("CYCLE", ",".join(rc["axes.prop_cycle"].by_key()["color"]))
        print("GRID", rc["axes.grid"], rc["grid.color"], rc["legend.frameon"], rc["axes.spines.top"])
        fig, ax = plt.subplots()
        ax.plot([0, 1, 2], [0, 1, 0])
        fig.savefig("plain.png")
        print("FIRST LINE", ax.lines[0].get_color())
        """
    )
    res = await runner.run(script)
    assert res.status == "ok", (res.stderr, res.violations)
    out = res.stdout.splitlines()
    assert f"RCFILE {RC_FILE}" in out
    assert "PAPER #FFFFFF #FFFFFF #FFFFFF" in out
    assert "INK #18181B #44403C #6B645C" in out
    assert "CYCLE " + ",".join(PLOT_CYCLE) in out
    assert "GRID True #E2DDD3 False False" in out
    assert f"FIRST LINE {PLOT_CYCLE[0]}" in out
    plain = next(a for a in res.artifacts if a.name == "plain.png")
    pixels = mpimg.imread(plain.store_path)[..., :3]
    border = [pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]]
    assert all(edge.min() == 1.0 for edge in border)  # white paper to every edge
    assert pixels.min() < 0.5  # with ink on it


def test_the_style_file_is_in_a_read_root_and_outside_every_write_root(runner, tmp_path):
    job = tmp_path / "jobs" / "eddie-sbx-probe"
    limits = json.loads(runner._limits_json(job))
    rc = Path(runner._env(job)["MATPLOTLIBRC"]).resolve()
    assert rc == RC_FILE.resolve() and rc.is_file()
    assert any(rc.is_relative_to(Path(root).resolve()) for root in limits["read_roots"])
    write_roots = [job, *map(Path, limits["extra_write_roots"])]
    assert not any(rc.is_relative_to(root.resolve()) for root in write_roots)


async def test_the_sandbox_cannot_change_the_style_file(runner):
    before = RC_FILE.read_bytes()
    # Opening for append or update changes nothing by itself, so a failed guard cannot hurt the repo.
    script = textwrap.dedent(
        f"""
        rc = {str(RC_FILE)!r}
        print("READ", len(open(rc).read()) > 0)
        for mode in ("a", "r+"):
            try:
                open(rc, mode).close()
                print("OPENED", mode)
            except PermissionError as exc:
                print("DENIED", mode, exc)
        """
    )
    res = await runner.run(script, postprocess=False)
    assert "READ True" in res.stdout
    assert "DENIED a" in res.stdout and "DENIED r+" in res.stdout and "OPENED" not in res.stdout
    assert res.status == "policy_violation"
    assert [v["event"] for v in res.violations] == ["write", "write"]
    assert all(v["detail"] == str(RC_FILE.resolve()) for v in res.violations)
    assert RC_FILE.read_bytes() == before


async def test_secrets_under_home_are_unreadable_and_env_is_scrubbed(runner, monkeypatch, offline_sandboxed_env):
    secret = offline_sandboxed_env / ".secrets" / "eddie" / "meta_api_key"
    secret.parent.mkdir(parents=True)
    secret.write_text("do-not-leak")
    monkeypatch.setenv("META_API_KEY", "env-do-not-leak")
    script = textwrap.dedent(
        f"""
        import os
        print("ENV", os.environ.get("META_API_KEY"))
        try:
            print("READ", open({str(secret)!r}).read())
        except PermissionError as exc:
            print("DENIED", exc)
        """
    )
    res = await runner.run(script, postprocess=False)
    assert "ENV None" in res.stdout
    assert "do-not-leak" not in res.stdout
    assert "DENIED" in res.stdout
    assert res.status == "policy_violation"


async def test_output_is_capped(runner):
    res = await runner.run("print('x' * 3_000_000)", postprocess=False)
    assert res.output_truncated
    assert len(res.stdout) <= 1_000_000


async def test_disallowed_extensions_are_not_collected(runner):
    res = await runner.run("open('payload.sh', 'w').write('rm -rf /')\nopen('ok.txt', 'w').write('fine')", postprocess=False)
    assert [a.name for a in res.artifacts] == ["ok.txt"]
    assert any("payload.sh" in r for r in res.rejected)


def test_stale_job_dirs_are_swept(tmp_path):
    old = tmp_path / "eddie-sbx-old"
    old.mkdir()
    os.utime(old, (0, 0))
    keep = tmp_path / "unrelated"
    keep.mkdir()
    assert sweep_stale(tmp_path) == 1
    assert not old.exists() and keep.exists()
