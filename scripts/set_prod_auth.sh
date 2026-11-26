#!/usr/bin/env bash
# Lock the private preview behind HTTP Basic Auth at the ingress, without the password ever
# touching a file, a command line, the shell history or the terminal.
#
#   scripts/set_prod_auth.sh            ask for the password (hidden, twice), store its hash, lock the ingress
#   scripts/set_prod_auth.sh --relock   put the Basic Auth annotations back (after an unlock); asks nothing
#   scripts/set_prod_auth.sh status     whether the Secret exists and whether the ingress is locked
#
# Run it yourself, in your own terminal. Values (NAMESPACE, AUTH_USER, AUTH_REALM, HOST,
# KUBE_CONTEXT) come from the git-ignored k8s/overlays/prod/deploy.env.
#
# Where the password travels: your keyboard -> this shell's memory -> a pipe -> htpasswd, which
# prints only a bcrypt hash (cost 8: ingress-nginx re-checks it on every request, on small ARM
# nodes) -> a pipe -> kubectl -> the API server over TLS. The password is never an argument of
# any process and is never written anywhere; only the hash is stored.
set -euo pipefail
case "$-" in *x*) echo "refusing to run with shell tracing on (set -x would print the password)" >&2; exit 2 ;; esac
ulimit -c 0 2>/dev/null || true

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAY="${EDDIE_OVERLAY:-$ROOT/k8s/overlays/prod}"
ENV_FILE="$OVERLAY/deploy.env"
KUBECTL="${KUBECTL:-kubectl}"
SECRET="eddie-basic-auth"
MIN_LENGTH=12

pw=""
pw2=""
line=""
trap 'pw=""; pw2=""; line=""; unset pw pw2 line' EXIT

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
confirm() {
  local answer=""
  printf '%s [y/N] ' "$1" >/dev/tty
  IFS= read -r answer </dev/tty || true
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

[[ -f "$ENV_FILE" ]] || die "no $ENV_FILE: run scripts/deploy_prod.sh init first"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
: "${KUBE_CONTEXT:?KUBE_CONTEXT is empty in deploy.env}" "${NAMESPACE:?}" "${HOST:?}"
AUTH_USER="${AUTH_USER:?AUTH_USER is empty in deploy.env}"
AUTH_REALM="${AUTH_REALM:-Eddie preview}"

k() { "$KUBECTL" --context "$KUBE_CONTEXT" "$@"; }
kn() { k -n "$NAMESPACE" "$@"; }
secret_exists() { kn get secret "$SECRET" >/dev/null 2>&1; }
ingress_exists() { kn get ingress eddie >/dev/null 2>&1; }

record_auth_on() {
  if grep -q '^AUTH=' "$ENV_FILE"; then sed -i 's/^AUTH=.*/AUTH=on/' "$ENV_FILE"; else echo "AUTH=on" >> "$ENV_FILE"; fi
  if grep -q '^PUBLIC_CACHE=' "$ENV_FILE"; then sed -i 's/^PUBLIC_CACHE=.*/PUBLIC_CACHE=0/' "$ENV_FILE"; fi
  "$ROOT/scripts/deploy_prod.sh" render >/dev/null
}

lock_ingress() {
  if ! ingress_exists; then
    say "the ingress does not exist yet: scripts/deploy_prod.sh apply will create it locked"
    return 0
  fi
  kn annotate ingress eddie --overwrite \
    nginx.ingress.kubernetes.io/auth-type=basic \
    "nginx.ingress.kubernetes.io/auth-secret=$SECRET" \
    "nginx.ingress.kubernetes.io/auth-realm=$AUTH_REALM" >/dev/null
  if kn get deploy eddie-web -o jsonpath='{.spec.template.spec.containers[0].env[*].name}' 2>/dev/null | grep -qw EDDIE_PUBLIC_CACHE; then
    kn set env deploy/eddie-web EDDIE_PUBLIC_CACHE- >/dev/null  # behind the wall, nothing is publicly cacheable
  fi
  say "locked: https://$HOST/ now asks for a login"
}

ensure_namespace() {
  k get namespace "$NAMESPACE" >/dev/null 2>&1 && return 0
  confirm "Namespace $NAMESPACE does not exist on $KUBE_CONTEXT. Create it (PodSecurity restricted)?" || die "not confirmed"
  k create namespace "$NAMESPACE" >/dev/null
  k label namespace "$NAMESPACE" app.kubernetes.io/part-of=eddie \
    pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/enforce-version=latest \
    pod-security.kubernetes.io/audit=restricted pod-security.kubernetes.io/warn=restricted >/dev/null
}

check_login() {  # 401 without a credential, 200 with it (the password goes to curl on stdin)
  if ! getent hosts "$HOST" >/dev/null 2>&1; then
    say "skipping the login check: $HOST does not resolve yet (create the DNS record first)"
    return 0
  fi
  sleep 3  # ingress-nginx reloads the credential within a few seconds
  local anon with
  anon="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOST/" || true)"
  with="$(printf 'user = "%s:%s"\n' "$AUTH_USER" "$pw" | curl -s --config - -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOST/" || true)"
  say "check: without a login -> $anon (want 401), with it -> $with (want 200 once the site is deployed)"
}

cmd_status() {
  say "cluster   $KUBE_CONTEXT, namespace $NAMESPACE"
  if secret_exists; then
    say "secret    $SECRET exists (created $(kn get secret "$SECRET" -o jsonpath='{.metadata.creationTimestamp}'))"
  else
    say "secret    $SECRET does not exist"
  fi
  if ingress_exists; then
    local type
    type="$(kn get ingress eddie -o jsonpath='{.metadata.annotations.nginx\.ingress\.kubernetes\.io/auth-type}')"
    say "ingress   ${type:+locked (auth-type $type)}${type:-OPEN: no Basic Auth}"
  else
    say "ingress   not deployed"
  fi
}

cmd_relock() {
  secret_exists || die "$NAMESPACE/$SECRET does not exist: run scripts/set_prod_auth.sh (no argument) to create it"
  lock_ingress
  record_auth_on
}

cmd_set() {
  [[ -r /dev/tty && -w /dev/tty ]] || die "needs a terminal: the password is typed, never piped or passed as an argument"
  command -v htpasswd >/dev/null || die "htpasswd is missing (apache2-utils)"
  say "cluster   $KUBE_CONTEXT, namespace $NAMESPACE, user $AUTH_USER"
  if secret_exists; then
    confirm "A preview password is already stored. Replace it?" || die "not confirmed"
  fi
  ensure_namespace
  printf 'Password for %s (hidden): ' "$AUTH_USER" >/dev/tty
  IFS= read -rs pw </dev/tty || true
  printf '\nAgain (hidden): ' >/dev/tty
  IFS= read -rs pw2 </dev/tty || true
  printf '\n' >/dev/tty
  [[ -n "$pw" ]] || die "nothing was entered"
  [[ "$pw" == "$pw2" ]] || die "the two entries differ (nothing was stored)"
  pw2=""
  (( ${#pw} >= MIN_LENGTH )) || die "use at least $MIN_LENGTH characters (nothing was stored)"
  # printf is a builtin, so the password is in no process's arguments; htpasswd reads it from stdin (-i)
  line="$(printf '%s\n' "$pw" | htpasswd -n -i -B -C 8 "$AUTH_USER" | head -n 1)"
  [[ "$line" == "$AUTH_USER:\$2"* ]] || die "htpasswd did not return a bcrypt line (nothing was stored)"
  # `create`, not `apply`: apply would keep a second copy of the hash in an annotation
  if secret_exists; then
    printf '%s\n' "$line" | kn create secret generic "$SECRET" --from-file=auth=/dev/stdin --dry-run=client -o yaml \
      | kn replace -f - >/dev/null
  else
    printf '%s\n' "$line" | kn create secret generic "$SECRET" --from-file=auth=/dev/stdin >/dev/null
  fi
  line=""
  kn label secret "$SECRET" app.kubernetes.io/part-of=eddie --overwrite >/dev/null
  say "stored    the bcrypt hash in $NAMESPACE/$SECRET (the password itself is stored nowhere)"
  lock_ingress
  record_auth_on
  check_login
  pw=""
}

case "${1:-set}" in
  set) cmd_set ;;
  --relock | relock) cmd_relock ;;
  status) cmd_status ;;
  -h | --help | help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' ;;
  # the argument is not echoed back: someone may have passed the password itself by mistake
  *) die "unknown command (use: set, --relock, status). The password is never an argument: if you just typed it, choose a new one." ;;
esac
