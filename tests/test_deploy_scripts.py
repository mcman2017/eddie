"""scripts/set_prod_auth.sh and scripts/deploy_prod.sh, driven against a fake kubectl.

What must hold: only a bcrypt hash of the preview password is stored, the password is never an
argument of any process or printed, mismatched or short entries store nothing, the ingress is
locked and AUTH=on recorded, and the rendered overlay carries the auth annotations. No cluster
is touched and no name resolves: the host is under .invalid.
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
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
