"""scripts/set_prod_auth.sh and scripts/deploy_prod.sh, driven against a fake kubectl.

What must hold: only a bcrypt hash of the preview password is stored, the password is never an
argument of any process or printed, mismatched or short entries store nothing, the ingress is
locked and AUTH=on recorded, and the rendered overlay carries the auth annotations. No cluster
is touched and no name resolves: the host is under .invalid.
"""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_ember_key_scripts import on_tty

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct-horse-battery-staple-42"

FAKE_KUBECTL = r"""#!/usr/bin/env bash
# One namespace, one ingress, one deployment, one secret; state in $FAKE_STATE.
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_STATE/argv.log"
args=" $* "
store="$FAKE_STATE/secret.b64"
case "$args" in
  *" get namespace "*) [[ -e "$FAKE_STATE/ns" ]] ;;
  *" create namespace "*) : > "$FAKE_STATE/ns" ;;
  *" label "*) : ;;
  *" create secret generic "*)
    value="$(base64 -w0)"
    if [[ "$args" == *"--dry-run=client"* ]]; then printf 'apiVersion: v1\nkind: Secret\ndata:\n  auth: %s\n' "$value"
    else [[ ! -e "$store" ]] || { echo "already exists" >&2; exit 1; }; printf '%s' "$value" > "$store"; fi ;;
  *" replace -f - "*) sed -n 's/^  auth: //p' > "$store" ;;
  *" get secret eddie-basic-auth"*) [[ -e "$store" ]] ;;
  *" get ingress eddie"*) [[ -e "$FAKE_STATE/ingress" ]] ;;
  *" annotate ingress eddie "*) printf '%s\n' "$*" > "$FAKE_STATE/annotations" ;;
  *" get deploy eddie-web "*) echo "EDDIE_BACKEND EDDIE_DATA_DIR" ;;
  *" set env "*) printf '%s\n' "$*" >> "$FAKE_STATE/setenv" ;;
  *) echo "fake kubectl: unhandled: $*" >&2; exit 64 ;;
esac
"""

DEPLOY_ENV = """KUBE_CONTEXT=test-ctx
NAMESPACE=eddie
HOST=eddie.invalid
IMAGE_REPO=registry.invalid/eddie
IMAGE_TAG=abc-1
PULL_SECRET=registry-secret
PULL_SECRET_FROM_NS=somewhere
STORAGE_CLASS=standard
PVC_SIZE=1Gi
CLUSTER_ISSUER=test-issuer
AUTH=off
AUTH_USER=preview
AUTH_REALM="Eddie preview"
PUBLIC_CACHE=1
"""


@pytest.fixture
def world(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "ingress").touch()
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "deploy.env").write_text(DEPLOY_ENV)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "KUBECTL": str(kubectl), "FAKE_STATE": str(state),
           "EDDIE_OVERLAY": str(overlay)}
    return state, overlay, env


AUTH = [str(ROOT / "scripts" / "set_prod_auth.sh")]


def stored(state: Path) -> str | None:
    f = state / "secret.b64"
    return base64.b64decode(f.read_text()).decode() if f.exists() else None


def test_only_a_bcrypt_hash_is_stored_and_the_ingress_is_locked(world, tmp_path):
    state, overlay, env = world
    code, out = on_tty(AUTH, env, [("Create it", "y"), ("Password for preview", PASSWORD), ("Again", PASSWORD)])
    assert code == 0, out
    line = stored(state).strip()
    assert line.startswith("preview:$2y$08$") and PASSWORD not in line  # bcrypt, cost 8
    pwfile = tmp_path / "htpasswd"
    pwfile.write_text(line + "\n")
    check = subprocess.run(["htpasswd", "-i", "-v", str(pwfile), "preview"], input=PASSWORD + "\n", text=True,
                           capture_output=True)
    assert check.returncode == 0, check.stderr  # the hash is of exactly what was typed
    argv = (state / "argv.log").read_text()
    assert PASSWORD not in argv and PASSWORD not in out
    assert "nginx.ingress.kubernetes.io/auth-type=basic" in (state / "annotations").read_text()
    assert "auth-secret=eddie-basic-auth" in (state / "annotations").read_text()
    assert "EDDIE_PUBLIC_CACHE" not in (state / "setenv").read_text() if (state / "setenv").exists() else True
    env_file = (overlay / "deploy.env").read_text()
    assert "AUTH=on" in env_file and "PUBLIC_CACHE=0" in env_file
    rendered = (overlay / "ingress.yaml").read_text()
    assert "auth-type, value: basic" in rendered and "eddie.invalid" in rendered


@pytest.mark.parametrize("first, second", [(PASSWORD, PASSWORD + "x"), ("short", "short")])
def test_a_mismatch_or_a_short_password_stores_nothing(world, first, second):
    state, _, env = world
    (state / "ns").touch()
    code, out = on_tty(AUTH, env, [("Password for preview", first), ("Again", second)])
    assert code != 0 and stored(state) is None
    assert first not in (state / "argv.log").read_text() if (state / "argv.log").exists() else True


def test_the_password_is_never_an_argument(world):
    state, _, env = world
    code, out = on_tty(AUTH + [PASSWORD], env, [])
    assert code != 0 and PASSWORD not in out and "never an argument" in out


def test_relock_needs_the_secret_and_annotates_again(world):
    state, overlay, env = world
    code, out = on_tty(AUTH + ["--relock"], env, [])
    assert code != 0 and "does not exist" in out
    (state / "secret.b64").write_text(base64.b64encode(b"preview:$2y$08$hash").decode())
    code, out = on_tty(AUTH + ["--relock"], env, [])
    assert code == 0, out
    assert "auth-type=basic" in (state / "annotations").read_text()
    assert "AUTH=on" in (overlay / "deploy.env").read_text()


def test_render_writes_the_overlay_from_deploy_env(world):
    _, overlay, env = world
    res = subprocess.run([str(ROOT / "scripts" / "deploy_prod.sh"), "render"], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    kustom = (overlay / "kustomization.yaml").read_text()
    assert "newName: registry.invalid/eddie" in kustom and 'newTag: "abc-1"' in kustom and "../../base" in kustom
    assert "auth-type" not in (overlay / "ingress.yaml").read_text()  # AUTH=off
    assert "EDDIE_PUBLIC_CACHE" in (overlay / "deployment.yaml").read_text()  # PUBLIC_CACHE=1
    assert 'storageClassName, value: "standard"' in (overlay / "pvc.yaml").read_text()


# --- sync: a data release into the volume, against a fake loader ----------------------------------

FAKE_KUBECTL_SYNC = r"""#!/usr/bin/env bash
# The loader pod's view of the volume is $FAKE_DATA: every /data in an exec'd command points there.
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_STATE/argv.log"
case " $* " in
  *" exec "*)
    while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
    shift
    cmd=()
    for a in "$@"; do cmd+=("${a//\/data/$FAKE_DATA}"); done
    exec "${cmd[@]}" ;;
  *" get pod -l "*) printf 'node-a' ;;
  *" create -f - "*) cat > "$FAKE_STATE/loader.yaml" ;;
  *" delete pod "* | *" wait "*) : ;;
  *) echo "fake kubectl: unhandled: $*" >&2; exit 64 ;;
esac
"""

# python -m eddie.cli release build --out DIR: three data files and their manifest. FAKE_VERSION
# changes one of them; FAKE_CORRUPT damages it after the manifest is written.
FAKE_RELEASE_BUILD = r"""#!/usr/bin/env bash
set -euo pipefail
out=""
while [[ $# -gt 0 ]]; do [[ "$1" == "--out" ]] && out="$2"; shift; done
mkdir -p "$out/index" "$out/media" "$out/state"
printf 'index bytes\n' > "$out/index/eddie_index.sqlite"
printf 'film bytes\n' > "$out/media/eddie_demo.mp4"
printf 'artifacts %s\n' "${FAKE_VERSION:-1}" > "$out/state/eddie_artifacts.sqlite"
printf '{"id": "fake"}\n' > "$out/RELEASE.json"
(cd "$out" && find . -type f ! -name MANIFEST.sha256 ! -name RELEASE.json | sed 's|^\./||' | sort | xargs sha256sum) > "$out/MANIFEST.sha256"
if [[ "${FAKE_CORRUPT:-0}" == 1 ]]; then printf 'x' >> "$out/state/eddie_artifacts.sqlite"; fi
"""


def _executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def volume(tmp_path):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "deploy_prod.sh", root / "scripts" / "deploy_prod.sh")
    (root / ".venv" / "bin").mkdir(parents=True)
    _executable(root / ".venv" / "bin" / "python", FAKE_RELEASE_BUILD)
    state, data, overlay = tmp_path / "state", tmp_path / "volume", tmp_path / "overlay"
    for d in (state, data, overlay):
        d.mkdir()
    (overlay / "deploy.env").write_text(DEPLOY_ENV)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "EDDIE_OVERLAY": str(overlay),
           "KUBECTL": str(_executable(tmp_path / "kubectl", FAKE_KUBECTL_SYNC)),
           "DOCKER": str(_executable(tmp_path / "docker", "#!/bin/sh\nexit 1\n")),  # no image: the ro check is skipped
           "FAKE_STATE": str(state), "FAKE_DATA": str(data)}
    runs = []

    def sync(**extra: str) -> subprocess.CompletedProcess:
        if runs:
            time.sleep(1.05)  # release ids are stamped to the second
        runs.append(1)
        return subprocess.run([str(root / "scripts" / "deploy_prod.sh"), "sync"], env={**env, **extra},
                              capture_output=True, text=True, timeout=60)

    return data, state, sync


def releases(data: Path) -> list[str]:
    return sorted(p.name for p in (data / "releases").iterdir())


def test_sync_sends_a_whole_first_release_then_only_what_changed(volume):
    data, state, sync = volume
    first = sync()
    assert first.returncode == 0, first.stderr
    live = data / "current"
    one = os.readlink(live)
    assert (live / "index/eddie_index.sqlite").read_text() == "index bytes\n"
    assert (live / "media/eddie_demo.mp4").is_file() and (live / ".complete").exists()
    assert "5 files sent, 0 linked" in first.stderr  # an empty volume gets everything (the NR == FNR bug sent 2)
    assert "nodeName: node-a" in (state / "loader.yaml").read_text()  # next to the web pod: the volume is RWO

    second = sync(FAKE_VERSION="2")
    assert second.returncode == 0, second.stderr
    assert "3 files sent, 2 linked" in second.stderr
    two = os.readlink(live)
    assert two != one and releases(data) == sorted([Path(one).name, Path(two).name])
    assert (data / two / "index/eddie_index.sqlite").stat().st_ino == (data / one / "index/eddie_index.sqlite").stat().st_ino
    assert (live / "state/eddie_artifacts.sqlite").read_text() == "artifacts 2\n"

    third = sync(FAKE_VERSION="3")
    assert third.returncode == 0, third.stderr
    assert releases(data) == sorted([Path(two).name, Path(os.readlink(live)).name])  # the oldest one is pruned


def test_a_sync_that_fails_its_checksums_keeps_the_live_release_and_drops_the_partial_one(volume):
    data, _, sync = volume
    assert sync().returncode == 0
    live = os.readlink(data / "current")
    bad = sync(FAKE_VERSION="2", FAKE_CORRUPT="1")
    assert bad.returncode != 0
    assert os.readlink(data / "current") == live
    assert releases(data) == [Path(live).name]
