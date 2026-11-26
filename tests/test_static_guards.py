"""Static guards over the source tree and infrastructure files."""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY_SOURCES = sorted((ROOT / "eddie").rglob("*.py"))


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def test_only_the_transport_names_the_ember_host():
    offenders = {_rel(p) for p in PY_SOURCES if "api.meta.ai" in p.read_text()}
    assert offenders <= {"eddie/llm/transport.py", "eddie/llm/adapters/ember.py"}, offenders
    assert "eddie/llm/transport.py" in offenders


def test_http_clients_are_created_only_in_adapters_and_the_fetcher():
    allowed = {"eddie/llm/adapters/ember.py", "eddie/llm/adapters/dgx.py", "eddie/ingest/fetch.py"}
    pattern = re.compile(r"httpx\.(?:Async)?Client\(|httpx\.(?:Async)?HTTPTransport\(")
    offenders = {_rel(p) for p in PY_SOURCES if pattern.search(p.read_text())}
    assert offenders <= allowed | {"eddie/llm/transport.py"}, offenders


def test_no_banned_libraries():
    for p in PY_SOURCES:
        text = p.read_text()
        assert not re.search(r"^\s*(?:import|from)\s+(?:streamlit|requests)\b", text, re.M), _rel(p)
    for manifest in ("pyproject.toml", "uv.lock", "web/package.json"):
        assert "streamlit" not in (ROOT / manifest).read_text().lower(), manifest


def test_api_does_not_import_the_llm_layer_directly():
    text = (ROOT / "eddie/api/app.py").read_text()
    assert not re.search(r"^\s*(?:from|import)\s+eddie\.llm", text, re.M)


def test_infrastructure_never_carries_keys_or_env_files():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "META_API_KEY" not in compose and "env_file" not in compose
    sources = [line.split("source:", 1)[1].strip() for line in compose.splitlines() if "source:" in line]
    assert sources == ["./data/index", "${EDDIE_PUBLIC_DIR:-${HOME}/.local/share/eddie/public}"], sources
    assert compose.count("read_only: true") >= 3  # both mounts + the root filesystem
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "k8s"],
                             capture_output=True, text=True).stdout.split()
    manifests = [ROOT / p for p in tracked if p.endswith((".yaml", ".yml"))] or list((ROOT / "k8s").rglob("*.yaml"))
    assert len(manifests) >= 8, manifests
    for manifest in manifests:
        text = manifest.read_text()
        assert "META_API_KEY" not in text, manifest.name
        assert "secretKeyRef" not in text and "kind: Secret" not in text, manifest.name
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "META_API_KEY" not in dockerfile and ".env" not in dockerfile
    dockerignore = (ROOT / ".dockerignore").read_text().split()
    for needed in (".env", "data/", "*.lean", "*.sqlite", "CLAUDE.local.md", "initial-prompt.md"):
        assert needed in dockerignore, needed


def test_gitignore_blocks_licensed_and_local_material():
    ignore = (ROOT / ".gitignore").read_text().split()
    for needed in ("*.lean", "*.tar*", "/data/*", "*.sqlite", ".env", "initial-prompt.md", "prompt-*.md",
                   ".hygiene-blocklist", "CLAUDE.local.md"):
        assert needed in ignore, needed


def _hygiene_module():
    spec = importlib.util.spec_from_file_location("hygiene", ROOT / "scripts/check_repo_hygiene.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "path",
    ["src/Main.lean", "data/raw/parts/lean.tar.gz.part01", "ledger.sqlite", "x.sqlite-wal", ".env",
     "initial-prompt.md", "prompt-1.md", "prompt-2.md", ".hygiene-blocklist", "CLAUDE.local.md",
     "exports/eddie_ember_telemetry.json", "data/index/x", "data/media/eddie_demo.mp4", "web/public/film.mp4",
     "web/public/clip.webm"],
)
def test_hygiene_check_flags_forbidden_paths(path):
    assert _hygiene_module().path_violations(path)


def test_hygiene_check_allows_examples():
    mod = _hygiene_module()
    assert mod.path_violations(".env.example") == []
    assert mod.path_violations("data/README.md") == []
    assert mod.path_violations("eddie/llm/ledger.py") == []


def test_hygiene_check_flags_secret_content():
    mod = _hygiene_module()
    fake_key = "sk-" + "a1B2" * 8
    assert mod.content_violations("notes.md", f"key = {fake_key}".encode())
    assert mod.content_violations("x.py", ("META_API_KEY" + '="' + "z" * 20 + '"').encode())
    assert mod.content_violations("x.md", ("someone" + "@" + "gmail.com").encode())
    assert mod.content_violations("x.md", b"nothing to see") == []


def test_tracked_tree_passes_the_hygiene_check():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    res = subprocess.run(["python3", str(ROOT / "scripts/check_repo_hygiene.py")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_hygiene_blocks_internal_hosts_by_hash_including_subdomains(monkeypatch):
    mod = _hygiene_module()
    assert mod.BLOCKED_HOST_SHA256, "the internal-host blocklist must not be empty"
    synthetic = "corp" + ".internal-example.test"
    monkeypatch.setattr(mod, "BLOCKED_HOST_SHA256", {mod._host_hash(synthetic)})
    assert mod.content_violations("k8s/x.yaml", f"image: registry.{synthetic}/app:1".encode())
    assert mod.content_violations("README.md", f"see https://{synthetic}/docs".encode())
    assert mod.content_violations("k8s/x.yaml", b"image: ghcr.io/mcman2017/eddie:latest") == []
    assert mod.content_violations("x.md", b"eddie.example.local and 127.0.0.1:8765") == []


def test_k8s_uses_only_public_placeholders():
    kustom = (ROOT / "k8s/base/kustomization.yaml").read_text()
    assert "ghcr.io/mcman2017/eddie" in kustom
    assert "eddie.example.local" in (ROOT / "k8s/base/ingress.yaml").read_text()
    assert "imagePullSecrets" not in (ROOT / "k8s/base/deployment.yaml").read_text()
    example = "".join(p.read_text() for p in (ROOT / "k8s/overlays/prod.example").iterdir() if p.is_file())
    assert "eddie.example.com" in example and "registry.example.com/eddie" in example


def test_the_real_overlay_is_git_ignored_and_the_pod_stays_read_only():
    res = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "k8s/overlays/prod/kustomization.yaml"])
    assert res.returncode == 0, "k8s/overlays/prod/ must be git-ignored: it names the real host"
    deployment = (ROOT / "k8s/base/deployment.yaml").read_text()
    assert "readOnly: true" in deployment and "claimName: eddie-data" in deployment
    assert "EDDIE_BACKEND\n              value: mock" in deployment


def test_deploy_scripts_never_take_a_password_on_the_command_line():
    for name in ("set_prod_auth.sh", "deploy_prod.sh", "unlock_public_submission.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert not re.search(r"^\s*set\s+(-[a-wyz]*x|-o\s+xtrace)", text, re.M), name  # tracing would print secrets
        assert 'case "$-" in *x*)' in text, name  # and each refuses to run under someone else's set -x
        assert not re.search(r"htpasswd[^\n]*\s-[a-zA-Z]*b", text), name  # -b would put the password in argv
    auth = (ROOT / "scripts/set_prod_auth.sh").read_text()
    assert "read -rs" in auth and "htpasswd -n -i -B -C 8" in auth and "/dev/tty" in auth
