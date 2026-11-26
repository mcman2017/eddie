"""scripts/ember-key.sh and scripts/ember-live.sh, driven on a pseudo-terminal against a fake kubectl.

What must hold: the key reaches the Secret exactly as typed (underscores and dashes included),
it is never an argument of any process and never printed, a malformed key stores nothing, and
the live wrapper hands the key to the CLI on an inherited pipe, never in an environment
variable (which /proc/<pid>/environ would keep for the process's life). No cluster is touched:
`kubectl` is a small script in the temp dir, and the "CLI" is a stub that reports what it saw.
"""

from __future__ import annotations

import base64
import os
import pty
import select
import shutil
import stat
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY = "Emb3r_test-key_0123-ABCD_wxyz-9876"  # synthetic: the alphabet a real key uses

FAKE_KUBECTL = r"""#!/usr/bin/env bash
# A stand-in for kubectl: one namespace, one secret, state in $FAKE_STATE.
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_STATE/argv.log"
args=" $* "
store="$FAKE_STATE/secret.b64"
case "$args" in
  *" config current-context "*) echo "test-cluster" ;;
  *" get namespace "*) [[ -e "$FAKE_STATE/ns" ]] ;;
  *" create namespace "*) : > "$FAKE_STATE/ns"; echo "namespace created" ;;
  *" label "* | *" annotate "*) : ;;
  *" create secret generic "*)
    value="$(base64 -w0)"
    if [[ "$args" == *"--dry-run=client"* ]]; then printf 'apiVersion: v1\nkind: Secret\ndata:\n  meta_api_key: %s\n' "$value"
    else [[ ! -e "$store" ]] || { echo "already exists" >&2; exit 1; }; printf '%s' "$value" > "$store"; fi ;;
  *" replace -f - "*) sed -n 's/^  meta_api_key: //p' > "$store" ;;
  *" delete secret "*) rm -f "$store"; echo "secret deleted" ;;
  *" get secret "*"creationTimestamp"*) [[ -e "$store" ]] && echo "2026-11-26T00:00:00Z" ;;
  *" get secret "*"jsonpath"*) cat "$store" ;;
  *" get secret "*) [[ -e "$store" ]] ;;
  *) echo "fake kubectl: unhandled: $*" >&2; exit 64 ;;
esac
"""


@pytest.fixture()
def world(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "KUBECTL": str(kubectl), "FAKE_STATE": str(state)}
    return state, env


def on_tty(argv: list[str], env: dict[str, str], answers: list[tuple[str, str]], timeout: float = 20.0) -> tuple[int, str]:
    """Run argv with a controlling terminal, typing each answer when its prompt appears."""
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        os.execvpe(argv[0], argv, env)
    out = b""
    asked = 0  # prompts are looked for after the previous answer only
    pending = list(answers)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:  # the child closed its side
                    break
                if not chunk:
                    break
                out += chunk
            if pending and pending[0][0].encode() in out[asked:]:
                time.sleep(0.3)  # a person is slower than `read -s` switching echo off
                os.write(fd, pending.pop(0)[1].encode() + b"\n")
                asked = len(out)
    finally:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def stored(state: Path) -> str | None:
    f = state / "secret.b64"
    return base64.b64decode(f.read_text()).decode() if f.exists() else None


SET = [str(ROOT / "scripts" / "ember-key.sh")]


@pytest.mark.slow
def test_key_is_stored_exactly_and_never_shown_or_passed_as_an_argument(world):
    state, env = world
    code, seen = on_tty(SET, env, [("Store the Ember API key", "y"), ("Create it", "y"), ("Ember API key", KEY)])
    assert code == 0, seen
    assert stored(state) == KEY  # underscores, dashes and case intact, no newline added
    assert KEY not in seen  # hidden input, and nothing echoes it afterwards
    assert f"{len(KEY)} characters" in seen and "read back and matched" in seen
    argv = (state / "argv.log").read_text()
    assert KEY not in argv and base64.b64encode(KEY.encode()).decode() not in argv
    assert "/dev/stdin" in argv and " apply " not in argv  # piped in; no last-applied annotation
    # nothing but the fake cluster's own store holds the key
    leaked = [p for p in state.parent.rglob("*") if p.is_file() and p.name != "secret.b64" and KEY in p.read_text(errors="ignore")]
    assert leaked == []


@pytest.mark.slow
def test_a_pasted_key_loses_its_stray_whitespace_and_a_second_run_replaces(world):
    state, env = world
    assert on_tty(SET, env, [("Store the Ember API key", "y"), ("Create it", "y"), ("Ember API key", f"  {KEY}\r")])[0] == 0
    assert stored(state) == KEY
    newer = KEY[::-1]
    code, seen = on_tty(SET, env, [("Replace it", "y"), ("Ember API key", newer)])
    assert code == 0 and stored(state) == newer and newer not in seen


@pytest.mark.slow
@pytest.mark.parametrize("bad", ["has a space_in-it-0123456789", "semi;colon_0123456789-abc", "quote\"_0123456789-abcdef", "short_key-1"])
def test_a_malformed_key_stores_nothing(world, bad):
    state, env = world
    code, seen = on_tty(SET, env, [("Store the Ember API key", "y"), ("Create it", "y"), ("Ember API key", bad)])
    assert code != 0 and stored(state) is None and "nothing was stored" in seen
    assert bad not in seen and not (state / "ns").exists()


@pytest.mark.slow
def test_the_key_is_never_taken_from_the_command_line(world):
    state, env = world
    code, seen = on_tty([*SET, KEY], env, [])
    assert code != 0 and stored(state) is None
    assert KEY not in seen  # a key passed by mistake is not echoed back either
    code, seen = on_tty([*SET, "set", KEY], env, [])
    assert code != 0 and stored(state) is None and KEY not in seen


@pytest.mark.slow
def test_refusing_at_a_prompt_changes_nothing(world):
    state, env = world
    code, _ = on_tty(SET, env, [("Store the Ember API key", "n")])
    assert code != 0 and stored(state) is None and not (state / "ns").exists()


@pytest.mark.slow
def test_status_and_remove(world):
    state, env = world
    assert on_tty([*SET, "status"], env, [])[0] != 0  # not set yet
    on_tty(SET, env, [("Store the Ember API key", "y"), ("Create it", "y"), ("Ember API key", KEY)])
    code, seen = on_tty([*SET, "status"], env, [])
    assert code == 0 and f"{len(KEY)} characters" in seen and KEY not in seen
    assert on_tty([*SET, "remove"], env, [("Delete the Secret", "y")])[0] == 0
    assert stored(state) is None


@pytest.mark.slow
def test_live_wrapper_hands_the_key_to_the_cli_on_a_pipe_never_in_the_environment(world, tmp_path):
    state, env = world
    on_tty(SET, env, [("Store the Ember API key", "y"), ("Create it", "y"), ("Ember API key", KEY)])
    # a copy of the repo layout whose "python" reports what the wrapper gave it
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "ember-live.sh", repo / "scripts" / "ember-live.sh")
    stub = repo / ".venv" / "bin" / "python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "argv=%s\\n" "$*"\n'
        'piped="$(cat <&"$EDDIE_KEY_FD")"\n'
        'printf "fd=%s piped_ok=%s env_key=%s backend=%s live=%s\\n" "$EDDIE_KEY_FD" '
        '"$([[ "$piped" == "$EXPECT" ]] && echo yes || echo no)" "${META_API_KEY:-unset}" "$EDDIE_BACKEND" "$EDDIE_ALLOW_LIVE_EMBER"\n'
        'env | grep -c "$EXPECT" || true\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    code, seen = on_tty([str(repo / "scripts" / "ember-live.sh"), "llm", "ping", "--region", "R1.triage"], {**env, "EXPECT": KEY}, [])
    assert code == 0, seen
    assert "argv=-m eddie.cli llm ping --region R1.triage" in seen  # the key is not among the arguments
    assert "fd=3 piped_ok=yes env_key=unset backend=ember live=true" in seen
    # the only environment variable holding the key is the test's own EXPECT: the wrapper adds none
    assert seen.strip().splitlines()[-1].strip() == "1"
    assert KEY not in seen and (state / "argv.log").read_text().count(KEY) == 0
    # without a stored key the wrapper stops before starting the CLI
    (state / "secret.b64").unlink()
    code, seen = on_tty([str(repo / "scripts" / "ember-live.sh"), "llm", "ping"], {**env, "EXPECT": KEY}, [])
    assert code != 0 and "argv=" not in seen and "ember-key.sh" in seen
