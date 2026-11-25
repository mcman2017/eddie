#!/usr/bin/env bash
# Put the Ember API key into a Kubernetes Secret without it ever touching a file, a command
# line, the shell history or the terminal.
#
#   scripts/ember-key.sh            ask for the key (hidden) and store or replace it
#   scripts/ember-key.sh status     say whether the Secret exists and how long the key is
#   scripts/ember-key.sh remove     delete the Secret
#
# Run it yourself, in your own terminal. Do not run it through an assistant or a CI job.
#
# Where the key travels: your keyboard -> this shell's memory -> a pipe -> kubectl -> the API
# server over TLS. It is never an argument of any process (arguments are visible in `ps`),
# never an environment variable here, and never written to disk by this script.
#
# The Secret lives in a namespace of its own that runs no workloads. Secrets are scoped to a
# namespace, so the read-only explorer (namespace `eddie`) cannot mount it: no container ever
# holds the key. The only reader is scripts/ember-live.sh, on the operator's host.
#
# Override with EDDIE_KEY_NAMESPACE, EDDIE_KEY_SECRET or KUBECTL if you need to.

set -euo pipefail
case "$-" in *x*) echo "refusing to run with shell tracing on (set -x would print the key)" >&2; exit 2 ;; esac
ulimit -c 0 2>/dev/null || true   # no core dump could carry the key

NS="${EDDIE_KEY_NAMESPACE:-eddie-operator}"
NAME="${EDDIE_KEY_SECRET:-ember-api-key}"
FIELD="meta_api_key"
KUBECTL="${KUBECTL:-kubectl}"

key=""
trap 'key=""; unset key' EXIT

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
k() { "$KUBECTL" "$@"; }

confirm() {
  local answer=""
  printf '%s [y/N] ' "$1" >/dev/tty
  IFS= read -r answer </dev/tty || true
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

exists() { k -n "$NS" get secret "$NAME" >/dev/null 2>&1; }

# The stored key, read back into memory only. Empty when there is none.
stored() { k -n "$NS" get secret "$NAME" -o "jsonpath={.data.$FIELD}" 2>/dev/null | base64 -d 2>/dev/null || true; }

digest() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

cmd_status() {
  say "cluster   $(k config current-context)"
  say "secret    $NS/$NAME (field $FIELD)"
  if ! exists; then
    say "state     not set"
    return 1
  fi
  key="$(stored)"
  say "state     set, ${#key} characters, created $(k -n "$NS" get secret "$NAME" -o 'jsonpath={.metadata.creationTimestamp}')"
}

cmd_remove() {
  exists || die "$NS/$NAME does not exist"
  confirm "Delete the Secret $NS/$NAME on $(k config current-context)?" || die "not confirmed"
  k -n "$NS" delete secret "$NAME" >&2
}

cmd_set() {
  [[ -r /dev/tty && -w /dev/tty ]] || die "needs a terminal: the key is typed, never piped or passed as an argument"
  [[ $# -eq 0 ]] || die "this command takes no arguments: the key is asked for, never passed on the command line"

  say "cluster   $(k config current-context)"
  say "secret    $NS/$NAME (field $FIELD)"
  if exists; then
    confirm "A key is already stored there. Replace it?" || die "not confirmed"
  else
    confirm "Store the Ember API key there?" || die "not confirmed"
  fi
  # every question comes before the key, so nothing a paste leaves behind can answer one
  local make_ns=0
  if ! k get namespace "$NS" >/dev/null 2>&1; then
    confirm "Namespace $NS does not exist. Create it (it will hold this Secret and no workloads)?" || die "not confirmed"
    make_ns=1
  fi

  printf 'Ember API key (hidden, paste and press Enter): ' >/dev/tty
  IFS= read -rs key </dev/tty || true
  printf '\n' >/dev/tty
  # a paste that ends in CR LF leaves a line behind: discard it
  local rest=""
  while IFS= read -rs -t 0.2 rest </dev/tty; do :; done
  # a paste can carry a carriage return or stray spaces at either end
  key="${key//$'\r'/}"
  key="${key#"${key%%[![:space:]]*}"}"
  key="${key%"${key##*[![:space:]]}"}"

  [[ -n "$key" ]] || die "nothing was entered"
  [[ "$key" =~ ^[A-Za-z0-9_-]+$ ]] || die "the key may only contain letters, digits, underscores and dashes (nothing was stored)"
  (( ${#key} >= 16 )) || die "that is only ${#key} characters, too short to be a key (nothing was stored)"

  if (( make_ns )); then
    k create namespace "$NS" >&2
    k label namespace "$NS" app.kubernetes.io/part-of=eddie eddie/role=operator-secrets >/dev/null
  fi

  # printf is a shell builtin, so the key is in no process's arguments; kubectl reads it from
  # the pipe. `create`, not `apply`: apply would keep a second copy in an annotation.
  if exists; then
    printf '%s' "$key" | k -n "$NS" create secret generic "$NAME" "--from-file=$FIELD=/dev/stdin" --dry-run=client -o yaml \
      | k -n "$NS" replace -f - >/dev/null
  else
    printf '%s' "$key" | k -n "$NS" create secret generic "$NAME" "--from-file=$FIELD=/dev/stdin" >/dev/null
  fi
  k -n "$NS" label secret "$NAME" app.kubernetes.io/part-of=eddie --overwrite >/dev/null
  k -n "$NS" annotate secret "$NAME" --overwrite \
    "eddie/use=Read on the operator host by scripts/ember-live.sh. Never mount it in a pod." >/dev/null

  # read it back and compare in memory, so a mangled paste is caught now and not at the first call
  [[ "$(digest "$(stored)")" == "$(digest "$key")" ]] || die "the stored value does not match what was typed"
  say "stored    ${#key} characters in $NS/$NAME, read back and matched"
  say "next      scripts/ember-live.sh llm ping --region R1.triage --effort minimal --max-output-tokens 16"
}

case "${1:-set}" in
  set) shift || true; cmd_set "$@" ;;
  status) cmd_status ;;
  remove) cmd_remove ;;
  -h | --help | help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' ;;
  # the argument is not echoed back: someone may have passed the key itself by mistake
  *) die "unknown command (use: set, status, remove). The key is never an argument: if you just typed it, rotate it." ;;
esac
