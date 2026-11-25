"""Run generated Python (numpy / mpmath / scipy / matplotlib) in a guarded subprocess.

Each job:
* gets a fresh `eddie-sbx-*` directory, always removed afterwards (stale ones are swept);
* runs `python -I -B boot.py` with an environment built from scratch: no API keys, no
  PYTHON* variables, HOME/TMPDIR inside the job, and MATPLOTLIBRC set to the packaged light
  plot style (`eddie.matplotlibrc`), so scripts need no styling code;
* has a new session, so a wall-clock timeout kills the whole process group;
* has stdout and stderr captured incrementally with a cap;
* is then post-processed (spectrograms + QC) under the same guards, and its allowed
  outputs are copied into the content-addressed artifact store.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from eddie import paths
from eddie.sandbox.artifacts import Artifact, CollectLimits, collect, sweep_stale
from eddie.sandbox.style import RC_FILE

BOOT = Path(__file__).with_name("boot.py")
POSTPROCESS = Path(__file__).with_name("postprocess.py")
PACKAGE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Limits:
    wall_s: float = 30.0
    cpu_s: int = 30
    mem_mb: int = 3072
    fsize_mb: int = 50
    nofile: int = 256
    max_output_bytes: int = 1_000_000


@dataclass
class SandboxResult:
    job_id: str
    status: str  # ok | error | timeout | policy_violation
    returncode: int | None
    stdout: str
    stderr: str
    duration_s: float
    output_truncated: bool = False
    violations: list[dict] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    qc: dict = field(default_factory=dict)

    @property
    def defects(self) -> dict[str, list[str]]:
        return {name: info.get("defects", []) for name, info in self.qc.items() if info.get("defects")}


class SandboxRunner:
    def __init__(
        self,
        artifact_dir: Path | None = None,
        *,
        limits: Limits = Limits(),
        tmp_root: Path | None = None,
        mpl_cache: Path | None = None,
        python: str = sys.executable,
    ) -> None:
        self.artifact_dir = Path(artifact_dir or paths.default_artifact_dir())
        self.limits = limits
        self.tmp_root = Path(tmp_root or tempfile.gettempdir())
        self.mpl_cache = Path(mpl_cache or paths.data_dir() / "sandbox" / "mplcache")
        self.python = python
        self.mpl_cache.mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        sweep_stale(self.tmp_root)
        self._warm_font_cache()

    def _warm_font_cache(self) -> None:
        """Build matplotlib's font cache once, outside the sandbox.

        On a cold cache matplotlib shells out to `fc-list`, which the sandbox (correctly)
        forbids. With a warm cache, sandboxed scripts never need a subprocess.
        """
        if any(self.mpl_cache.glob("fontlist-*.json")):
            return
        import subprocess

        subprocess.run(
            [self.python, "-I", "-B", "-c", "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot"],
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.mpl_cache), "MPLCONFIGDIR": str(self.mpl_cache), "LANG": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=180,
            check=False,
        )

    def _env(self, job: Path) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(job),
            "TMPDIR": str(job / "tmp"),
            "MPLCONFIGDIR": str(self.mpl_cache),
            "MPLBACKEND": "Agg",
            # The house plot style. The file is in the package (a read root, never a write root),
            # so sandboxed code gets the style and cannot change it for the next job.
            "MATPLOTLIBRC": str(RC_FILE),
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "LANG": "C.UTF-8",
        }

    def _limits_json(self, job: Path) -> str:
        return json.dumps(
            {
                "cpu_s": self.limits.cpu_s,
                "mem_mb": self.limits.mem_mb,
                "fsize_mb": self.limits.fsize_mb,
                "nofile": self.limits.nofile,
                "home_deny": os.environ.get("HOME") or str(Path.home()),
                "read_roots": [sys.prefix, sys.base_prefix, str(PACKAGE_DIR), str(self.mpl_cache)],
                "extra_write_roots": [str(self.mpl_cache)],
            }
        )

    async def _exec(self, job: Path, argv: list[str], wall_s: float) -> tuple[int | None, bytes, bytes, bool, bool]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=job,
            env=self._env(job),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        cap = self.limits.max_output_bytes
        out, err = bytearray(), bytearray()
        truncated = [False]

        async def drain(stream: asyncio.StreamReader, buf: bytearray) -> None:
            while chunk := await stream.read(65_536):
                room = cap - len(buf)
                if room > 0:
                    buf.extend(chunk[:room])
                if len(chunk) > max(room, 0):
                    truncated[0] = True

        timed_out = False
        tasks = [asyncio.create_task(drain(proc.stdout, out)), asyncio.create_task(drain(proc.stderr, err))]
        try:
            await asyncio.wait_for(asyncio.gather(proc.wait(), *tasks), wall_s)
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return proc.returncode, bytes(out), bytes(err), timed_out, truncated[0]

    async def run(self, script: str, *, timeout_s: float | None = None, postprocess: bool = True) -> SandboxResult:
        job = Path(tempfile.mkdtemp(prefix="eddie-sbx-", dir=self.tmp_root))
        job_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        try:
            (job / "tmp").mkdir()
            (job / "main.py").write_text(script, encoding="utf-8")
            timeout = timeout_s or self.limits.wall_s
            argv = [self.python, "-I", "-B", str(BOOT), str(job), self._limits_json(job)]
            rc, out, err, timed_out, truncated = await self._exec(job, argv, timeout)
            violations = _read_violations(job)
            if timed_out:
                status = "timeout"
            elif violations:
                status = "policy_violation"
            elif rc == 0:
                status = "ok"
            else:
                status = "error"
            qc: dict = {}
            if postprocess and not timed_out:
                (job / ".sandbox_violations").unlink(missing_ok=True)
                post_argv = argv + ["--post", str(POSTPROCESS)]
                await self._exec(job, post_argv, max(timeout, 60.0))
                qc_file = job / "qc.json"
                if qc_file.exists():
                    qc = json.loads(qc_file.read_text())
                violations += [dict(v, phase="postprocess") for v in _read_violations(job)]
            artifacts, rejected = collect(job, self.artifact_dir, CollectLimits())
            return SandboxResult(
                job_id=job_id,
                status=status,
                returncode=rc,
                stdout=out.decode("utf-8", "replace"),
                stderr=err.decode("utf-8", "replace"),
                duration_s=round(time.monotonic() - started, 3),
                output_truncated=truncated,
                violations=violations,
                artifacts=artifacts,
                rejected=rejected,
                qc=qc,
            )
        finally:
            shutil.rmtree(job, ignore_errors=True)


def _read_violations(job: Path) -> list[dict]:
    path = job / ".sandbox_violations"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"event": "unparseable", "detail": line[:200]})
    return out
