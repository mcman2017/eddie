#!/usr/bin/env bash
# Deploy Eddie's keyless, read-only explorer to a Kubernetes cluster.
#
#   scripts/deploy_prod.sh init      write k8s/overlays/prod/deploy.env from the example (then edit it)
#   scripts/deploy_prod.sh render    write the git-ignored overlay from deploy.env (--example: the tracked example)
#   scripts/deploy_prod.sh check     tools, kube context, render, server-side dry run; applies nothing
#   scripts/deploy_prod.sh image     build the web image here (arm64) and push it; records IMAGE_TAG
#   scripts/deploy_prod.sh apply     diff, then apply (namespace, volume, Deployment, Service, Ingress, policies)
#   scripts/deploy_prod.sh sync      ship a data release into the volume, swapped atomically (no downtime)
#   scripts/deploy_prod.sh status    pods, volume, ingress, certificate, the current release
#   scripts/deploy_prod.sh smoke     checks inside the pod and from outside (401 while locked, 200 after)
#
# The real host, registry and kube context live only in the git-ignored k8s/overlays/prod/. This
# script never reads, mounts or ships the Ember key or the spend ledger: the pod serves data only.
set -euo pipefail
case "$-" in *x*) echo "refusing to run with shell tracing on" >&2; exit 2 ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="$ROOT/k8s/overlays/prod.example"
OVERLAY="${EDDIE_OVERLAY:-$ROOT/k8s/overlays/prod}"
ENV_FILE="$OVERLAY/deploy.env"
PY="$ROOT/.venv/bin/python"
KUBECTL="${KUBECTL:-kubectl}"
DOCKER="${DOCKER:-docker}"
LOADER="eddie-loader"
WEB_SELECTOR="app.kubernetes.io/name=eddie,app.kubernetes.io/component=web"

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
confirm() {
  [[ "${YES:-0}" == 1 ]] && return 0
  local answer=""
  printf '%s [y/N] ' "$1" >/dev/tty
  IFS= read -r answer </dev/tty || true
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

load_env() {
  local file="${1:-$ENV_FILE}"
  [[ -f "$file" ]] || die "no $file: run scripts/deploy_prod.sh init, then edit it"
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
  for v in KUBE_CONTEXT NAMESPACE HOST IMAGE_REPO PULL_SECRET PULL_SECRET_FROM_NS STORAGE_CLASS PVC_SIZE CLUSTER_ISSUER AUTH; do
    [[ -n "${!v:-}" ]] || die "$v is empty in $file"
  done
  AUTH_REALM="${AUTH_REALM:-Eddie preview}"
  PUBLIC_CACHE="${PUBLIC_CACHE:-0}"
  IMAGE_TAG="${IMAGE_TAG:-}"
  IMAGE_PATCH_SKIP_ANNOTATION="${IMAGE_PATCH_SKIP_ANNOTATION:-}"
}

k() { "$KUBECTL" --context "$KUBE_CONTEXT" "$@"; }
kn() { k -n "$NAMESPACE" "$@"; }

set_env_value() {  # set_env_value KEY VALUE (in deploy.env, in place)
  local key="$1" value="$2"
  if grep -q "^$key=" "$ENV_FILE"; then
    sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

# --- the overlay ---------------------------------------------------------------------------------

render_into() {  # render_into DIR: the kustomization and its JSON patches, from the loaded variables
  local dir="$1" tag="${IMAGE_TAG:-latest}"
  mkdir -p "$dir"
  cat > "$dir/kustomization.yaml" <<EOF
# Rendered by scripts/deploy_prod.sh render from deploy.env. Do not edit: re-render instead.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NAMESPACE
resources:
  - ../../base
images:
  - name: ghcr.io/mcman2017/eddie
    newName: $IMAGE_REPO
    newTag: "$tag"
patches:
  - target: {kind: Ingress, name: eddie}
    path: ingress.yaml
  - target: {kind: Deployment, name: eddie-web}
    path: deployment.yaml
  - target: {kind: PersistentVolumeClaim, name: eddie-data}
    path: pvc.yaml
EOF
  {
    cat <<EOF
# JSON patch for the base Ingress: the real host, TLS from cert-manager, and (while AUTH=on) Basic Auth.
- {op: replace, path: /spec/rules/0/host, value: "$HOST"}
- op: add
  path: /spec/tls
  value: [{hosts: ["$HOST"], secretName: eddie-tls}]
- {op: add, path: /metadata/annotations/cert-manager.io~1cluster-issuer, value: "$CLUSTER_ISSUER"}
EOF
    if [[ "$AUTH" == "on" ]]; then
      cat <<EOF
- {op: add, path: /metadata/annotations/nginx.ingress.kubernetes.io~1auth-type, value: basic}
- {op: add, path: /metadata/annotations/nginx.ingress.kubernetes.io~1auth-secret, value: eddie-basic-auth}
- {op: add, path: /metadata/annotations/nginx.ingress.kubernetes.io~1auth-realm, value: "$AUTH_REALM"}
EOF
    fi
  } > "$dir/ingress.yaml"
  {
    cat <<EOF
# JSON patch for the base Deployment: the pull secret, and the switches that change after the unlock.
- op: add
  path: /spec/template/spec/imagePullSecrets
  value: [{name: "$PULL_SECRET"}]
EOF
    if [[ "$PUBLIC_CACHE" == "1" ]]; then
      echo '- {op: add, path: /spec/template/spec/containers/0/env/-, value: {name: EDDIE_PUBLIC_CACHE, value: "1"}}'
    fi
    if [[ -n "$IMAGE_PATCH_SKIP_ANNOTATION" ]]; then
      echo "- {op: add, path: /metadata/annotations, value: {\"$IMAGE_PATCH_SKIP_ANNOTATION\": \"true\"}}"
    fi
  } > "$dir/deployment.yaml"
  cat > "$dir/pvc.yaml" <<EOF
# JSON patch for the data volume: this cluster's storage class and size.
- {op: add, path: /spec/storageClassName, value: "$STORAGE_CLASS"}
- {op: replace, path: /spec/resources/requests/storage, value: "$PVC_SIZE"}
EOF
}

cmd_init() {
  mkdir -p "$OVERLAY"
  [[ ! -e "$ENV_FILE" ]] || die "$ENV_FILE already exists"
  cp "$EXAMPLE/deploy.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  say "wrote $ENV_FILE: fill in the real values, then run scripts/deploy_prod.sh check"
}

cmd_render() {
  if [[ "${1:-}" == "--example" ]]; then
    load_env "$EXAMPLE/deploy.env.example"
    render_into "$EXAMPLE"
    say "rendered the tracked example overlay (placeholders only)"
    return
  fi
  load_env
  if [[ "$OVERLAY" == "$ROOT"/* ]]; then  # inside the repo the overlay must be git-ignored: it names the real host
    git -C "$ROOT" check-ignore -q "$OVERLAY/kustomization.yaml" || die "$OVERLAY is not git-ignored; refusing to write real values there"
  fi
  render_into "$OVERLAY"
  say "rendered $OVERLAY (AUTH=$AUTH, PUBLIC_CACHE=$PUBLIC_CACHE, image ${IMAGE_REPO##*/}:${IMAGE_TAG:-latest})"
}

rendered() { "$KUBECTL" kustomize "$OVERLAY"; }

cmd_check() {
  cmd_render
  for t in "$KUBECTL" "$DOCKER" jq tar sha256sum htpasswd curl; do
    command -v "$t" >/dev/null || die "missing tool: $t"
  done
  k config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 || die "no kube context $KUBE_CONTEXT"
  local out
  out="$(rendered)"
  if grep -qE 'kind: Secret|secretKeyRef|META_API_KEY' <<<"$out"; then
    die "the rendered overlay carries a Secret or a key reference; the pod must stay keyless"
  fi
  if k get namespace "$NAMESPACE" >/dev/null 2>&1; then
    rendered | k apply --dry-run=server -f - >/dev/null
    say "server-side dry run: ok (namespace $NAMESPACE exists)"
  else
    rendered | k apply --dry-run=client -f - >/dev/null
    say "client-side dry run: ok (namespace $NAMESPACE does not exist yet: set_prod_auth.sh or apply creates it)"
  fi
  [[ -n "$IMAGE_TAG" ]] || say "note: no IMAGE_TAG yet: run scripts/deploy_prod.sh image"
  say "check passed: nothing was applied"
}

# --- the image -----------------------------------------------------------------------------------

cmd_image() {
  load_env
  local tag
  tag="$(git -C "$ROOT" rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M)"
  if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]]; then
    say "warning: the tree has uncommitted changes; the image will include them"
  fi
  "$DOCKER" build -t "$IMAGE_REPO:$tag" "$ROOT"
  "$DOCKER" push "$IMAGE_REPO:$tag"
  set_env_value IMAGE_TAG "$tag"
  IMAGE_TAG="$tag"
  render_into "$OVERLAY"
  say "pushed ${IMAGE_REPO##*/}:$tag and recorded it in deploy.env"
}

# --- apply ---------------------------------------------------------------------------------------

copy_pull_secret() {
  kn get secret "$PULL_SECRET" >/dev/null 2>&1 && return 0
  k -n "$PULL_SECRET_FROM_NS" get secret "$PULL_SECRET" -o json \
    | jq --arg ns "$NAMESPACE" '{apiVersion, kind, type, data,
        metadata: {name: .metadata.name, namespace: $ns, labels: {"app.kubernetes.io/part-of": "eddie"}}}' \
    | k create -f - >/dev/null
  say "copied the pull secret $PULL_SECRET from $PULL_SECRET_FROM_NS (piped, never printed)"
}

cmd_apply() {
  cmd_render
  [[ -n "$IMAGE_TAG" ]] || die "no IMAGE_TAG: run scripts/deploy_prod.sh image first"
  if [[ "$AUTH" == "on" ]]; then
    kn get secret eddie-basic-auth >/dev/null 2>&1 \
      || die "AUTH=on but $NAMESPACE/eddie-basic-auth does not exist: run scripts/set_prod_auth.sh first"
  fi
  rendered | k apply -f - --dry-run=server >/dev/null
  rendered | k diff -f - || true
  confirm "Apply this to $KUBE_CONTEXT?" || die "not confirmed"
  rendered | k apply -f - --server-side --field-manager=eddie-deploy >/dev/null
  copy_pull_secret
  kn rollout status deploy/eddie-web --timeout=240s
}

# --- sync: a data release into the volume ----------------------------------------------------------

verify_release_ro() {  # every database opens mode=ro on a read-only mount, creating nothing
  local rel="$1" img="$IMAGE_REPO:${IMAGE_TAG:-latest}"
  "$DOCKER" image inspect "$img" >/dev/null 2>&1 || img="eddie-sandbox:local"
  "$DOCKER" image inspect "$img" >/dev/null 2>&1 || { say "warning: no local image to check the read-only open with"; return 0; }
  "$DOCKER" run --rm -i --network none --read-only --cap-drop ALL --user 65534:65534 \
    --mount "type=bind,source=$rel,target=/data/current,readonly" "$img" python -I - <<'EOF'
import pathlib, sqlite3
dbs = sorted(pathlib.Path("/data/current").rglob("*.sqlite"))
for db in dbs:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete", db
    con.close()
print(f"read-only open: {len(dbs)} databases ok")
EOF
}

loader_manifest() {
  local node="$1" image="$IMAGE_REPO:$IMAGE_TAG"
  cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $LOADER
  labels: {app.kubernetes.io/name: eddie-loader, app.kubernetes.io/part-of: eddie}
spec:
  ${node:+nodeName: $node}
  restartPolicy: Never
  activeDeadlineSeconds: 900
  automountServiceAccountToken: false
  imagePullSecrets: [{name: "$PULL_SECRET"}]
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: loader
      image: "$image"
      command: ["sleep", "900"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      resources:
        requests: {cpu: 50m, memory: 64Mi}
        limits: {cpu: "1", memory: 256Mi}
      volumeMounts:
        - {name: data, mountPath: /data}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: eddie-data}
    - name: tmp
      emptyDir: {sizeLimit: 64Mi}
EOF
}

cmd_sync() {
  load_env
  [[ -n "$IMAGE_TAG" ]] || die "no IMAGE_TAG: run scripts/deploy_prod.sh image first"
  local id rel node
  id="$(date -u +%Y%m%dT%H%M%SZ)"
  rel="$ROOT/data/deploy/$id"
  "$PY" -m eddie.cli release build --out "$rel"
  verify_release_ro "$rel"
  node="$(kn get pod -l "$WEB_SELECTOR" -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)"
  kn delete pod "$LOADER" --ignore-not-found --wait=true >/dev/null
  loader_manifest "$node" | kn create -f - >/dev/null
  trap 'kn delete pod "$LOADER" --ignore-not-found --wait=false >/dev/null 2>&1 || true' EXIT
  kn wait --for=condition=Ready "pod/$LOADER" --timeout=240s >/dev/null
  # files whose sha256 the current release already has are hard-linked inside the volume, not sent
  local remote changed unchanged
  remote="$(mktemp)"; changed="$(mktemp)"; unchanged="$(mktemp)"
  kn exec "$LOADER" -- sh -c 'cat /data/current/MANIFEST.sha256 2>/dev/null || true' > "$remote"
  awk 'NR == FNR { have[$0] = 1; next } { if (($0 in have) && $2 != "MANIFEST.sha256") print $2 > "'"$unchanged"'"; else print $2 > "'"$changed"'" }' \
    "$remote" "$rel/MANIFEST.sha256"
  printf '%s\n' MANIFEST.sha256 RELEASE.json >> "$changed"
  touch "$unchanged"
  kn exec "$LOADER" -- mkdir -p "/data/releases/$id"
  if [[ -s "$unchanged" ]]; then
    kn exec -i "$LOADER" -- sh -c "cd /data && while IFS= read -r f; do mkdir -p \"releases/$id/\$(dirname \"\$f\")\" && ln \"current/\$f\" \"releases/$id/\$f\"; done" < "$unchanged"
  fi
  sort -u "$changed" -o "$changed"
  tar -C "$rel" -cf - -T "$changed" | kn exec -i "$LOADER" -- tar -C "/data/releases/$id" -xf -
  kn exec "$LOADER" -- sh -c "cd /data/releases/$id && sha256sum -c --quiet MANIFEST.sha256"
  kn exec "$LOADER" -- sh -c "cd /data && ln -sfn releases/$id current.new && mv -T current.new current"
  kn exec "$LOADER" -- sh -c 'cd /data/releases && ls -1t | tail -n +3 | xargs -r rm -rf'
  say "release $id is live: $(wc -l < "$changed") files sent, $(wc -l < "$unchanged") linked from the previous release"
  rm -f "$remote" "$changed" "$unchanged"
  ls -1dt "$ROOT"/data/deploy/20*/ 2>/dev/null | tail -n +3 | xargs -r rm -rf
}

# --- status and smoke ------------------------------------------------------------------------------

cmd_status() {
  load_env
  kn get pods,pvc,svc,ingress -o wide
  kn get certificate 2>/dev/null || true
  kn exec deploy/eddie-web -- sh -c 'printf "current release: %s\n" "$(readlink /data/current)"' 2>/dev/null || true
  curl -s -o /dev/null -w "public https://$HOST/ -> %{http_code}\n" --max-time 15 "https://$HOST/" || true
}

cmd_smoke() {
  load_env
  kn exec -i deploy/eddie-web -- python - <<'EOF'
import json, urllib.request
def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8765" + path, timeout=15) as r:
        return json.load(r)
chapters = get("/api/chapters")["chapters"]
film = get("/api/demo-video/meta")
print(f"in the pod: healthz ok={get('/healthz')['ok']}, {len(chapters)} chapters "
      f"({sum(c['source'] == 'ember' for c in chapters)} written by Ember), film {film.get('mode') or 'absent'}")
EOF
  local code want
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOST/healthz" || true)"
  want=$([[ "$AUTH" == "on" ]] && echo 401 || echo 200)
  say "from outside: https://$HOST/healthz -> $code (expected $want)"
  [[ "$code" == "$want" ]] || die "unexpected status"
  if [[ "${1:-}" == "--with-password" && "$AUTH" == "on" ]]; then
    local pw=""
    printf 'Password for %s (hidden): ' "${AUTH_USER:-the preview user}" >/dev/tty
    IFS= read -rs pw </dev/tty; printf '\n' >/dev/tty
    code="$(printf 'user = "%s:%s"\n' "${AUTH_USER:?set AUTH_USER in deploy.env}" "$pw" \
      | curl -s --config - -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOST/")"
    pw=""
    say "with the password: https://$HOST/ -> $code (expected 200)"
    [[ "$code" == 200 ]] || die "unexpected status"
  fi
}

case "${1:-}" in
  init) cmd_init ;;
  render) shift; cmd_render "$@" ;;
  check) cmd_check ;;
  image) cmd_image ;;
  apply) cmd_apply ;;
  sync) cmd_sync ;;
  status) cmd_status ;;
  smoke) shift; cmd_smoke "$@" ;;
  -h | --help | help | "") sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command $1 (init, render, check, image, apply, sync, status, smoke)" ;;
esac
