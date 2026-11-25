"""Sandbox child bootstrap (stdlib only). Run as: python -I -B boot.py JOB_DIR LIMITS_JSON [--post SCRIPT].

Before any untrusted code runs it:
1. sets rlimits (CPU, address space, file size, open files, no core dumps) and umask 077;
2. installs a PEP 578 audit hook that denies network, process creation, writes outside the
   job dir (and the shared matplotlib cache), and reads of the user's home outside the venv,
   the eddie package and the job dir (`~/.secrets`, `~/.config/eddie`, the ledger, `.env`, ...).

Each denial is recorded to a pre-opened file and raised as PermissionError. This guards
against buggy or prompt-injected generated code. It is not a boundary against native-code
attackers; a Docker `--network none` backend is the planned hardening.
"""

from __future__ import annotations

import json
import os
import resource
import runpy
import signal
import sys

_NET_EVENTS = frozenset(
    {
        "socket.__new__", "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
        "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
        "http.client.connect", "urllib.Request", "ftplib.connect", "smtplib.connect", "webbrowser.open",
    }
)
_PROC_EVENTS = frozenset(
    {
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
        "os.forkpty", "os.kill", "os.killpg", "signal.pthread_kill", "pty.spawn", "os.startfile",
    }
)
_PATH_MUTATIONS = frozenset(
    {
        "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate",
        "shutil.rmtree", "os.utime", "shutil.copyfile", "shutil.copymode", "shutil.copystat",
        "shutil.copytree", "shutil.move",
    }
)
_ALWAYS_DENY = frozenset({"os.link", "os.symlink", "os.chroot", "os.setuid", "os.setgid"})


def _under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def install(job_dir: str, limits: dict) -> None:
    job_dir = os.path.realpath(job_dir)
    write_roots = [job_dir] + [os.path.realpath(p) for p in limits.get("extra_write_roots", [])]
    home = limits.get("home_deny")
    home = os.path.realpath(home) if home else None
    read_roots = [os.path.realpath(p) for p in limits.get("read_roots", [])] + write_roots
    viol_fd = os.open(os.path.join(job_dir, ".sandbox_violations"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    def deny(event: str, detail: object) -> None:
        line = json.dumps({"event": event, "detail": str(detail)[:300]}) + "\n"
        os.write(viol_fd, line.encode())
        raise PermissionError(f"eddie sandbox: {event} denied ({str(detail)[:120]})")

    def resolve(target: object) -> str | None:
        if isinstance(target, int):
            return None
        if isinstance(target, bytes):
            target = target.decode(errors="replace")
        if isinstance(target, os.PathLike):
            target = os.fspath(target)
        if not isinstance(target, str):
            return None
        return os.path.realpath(target)

    def hook(event: str, args: tuple) -> None:
        if event in _NET_EVENTS:
            if event == "socket.connect" or event == "socket.bind" or event == "socket.sendto":
                detail = args[1] if len(args) > 1 else args  # the address, not the socket object
            else:
                detail = args[:2]  # (host, port) for lookups
            deny(event, detail)
        if event in _PROC_EVENTS or event in _ALWAYS_DENY:
            deny(event, args[:1])
        if event == "open":
            path = resolve(args[0])
            if path is None:
                return
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
            )
            if writing and not _under(path, write_roots) and path != "/dev/null":
                deny("write", path)
            if home and _under(path, [home]) and not _under(path, read_roots):
                deny("read", path)
        elif event in _PATH_MUTATIONS:
            for arg in args[:2]:
                path = resolve(arg)
                if path is not None and not _under(path, write_roots):
                    deny(event, path)
        elif event in ("os.listdir", "os.scandir", "os.chdir", "sqlite3.connect"):
            path = resolve(args[0]) if args else None
            if path is None:
                return
            if event == "os.chdir" and not _under(path, write_roots):
                deny(event, path)
            if home and _under(path, [home]) and not _under(path, read_roots):
                deny(event, path)

    sys.addaudithook(hook)


def apply_limits(limits: dict) -> None:
    cpu = int(limits.get("cpu_s", 30))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    mem = int(limits.get("mem_mb", 3072)) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    fsize = int(limits.get("fsize_mb", 50)) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # oversize writes raise OSError instead of killing
    nofile = int(limits.get("nofile", 256))
    resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)


def main() -> None:
    job_dir, limits = sys.argv[1], json.loads(sys.argv[2])
    post = sys.argv[4] if len(sys.argv) > 4 and sys.argv[3] == "--post" else None
    apply_limits(limits)
    os.chdir(job_dir)
    install(job_dir, limits)
    script = post or os.path.join(job_dir, "main.py")
    sys.argv = [script, job_dir]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
