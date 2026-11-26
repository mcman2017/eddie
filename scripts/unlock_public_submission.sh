#!/usr/bin/env bash
# Submission day: take the Basic Auth wall down so judges can open the site.
#
#   scripts/unlock_public_submission.sh [--youtube <url-or-id>] [--no-freeze-check]
#
# Before anything changes it checks that the Ember key is frozen (the kill switch exists: run
# `uv run python -m eddie.cli budget trip --reason "submission freeze"` first), that the final
# telemetry export is newer than the last ledger call, and that the kube context is deploy.env's.
# With --youtube it first switches the film to the public YouTube upload and ships that (the MP4
# then stops being served from here). Then, after you type UNLOCK, it removes the Basic Auth
# annotations, turns on public cache headers, records AUTH=off and PUBLIC_CACHE=1 in deploy.env
# (so a later deploy stays public) and checks that the site answers 200 without a login.
# The credential Secret is kept: scripts/set_prod_auth.sh --relock puts the wall back.
set -euo pipefail
case "$-" in *x*) echo "refusing to run with shell tracing on" >&2; exit 2 ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAY="${EDDIE_OVERLAY:-$ROOT/k8s/overlays/prod}"
ENV_FILE="$OVERLAY/deploy.env"
KUBECTL="${KUBECTL:-kubectl}"
PY="$ROOT/.venv/bin/python"
STATE="${XDG_DATA_HOME:-$HOME/.local/share}/eddie"
EXPORT="${EDDIE_EXPORTS_DIR:-$ROOT/exports}/eddie_ember_telemetry.json"
SIDECAR="${EDDIE_MEDIA_DIR:-$ROOT/data/media}/eddie_demo.json"

youtube=""
freeze_check=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --youtube) youtube="${2:?--youtube needs a URL or an ID}"; shift 2 ;;
    --no-freeze-check) freeze_check=0; shift ;;
    -h | --help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
warn() { say "WARNING: $*"; }

[[ -f "$ENV_FILE" ]] || die "no $ENV_FILE"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
: "${KUBE_CONTEXT:?}" "${NAMESPACE:?}" "${HOST:?}"
k() { "$KUBECTL" --context "$KUBE_CONTEXT" "$@"; }
kn() { k -n "$NAMESPACE" "$@"; }

# --- preconditions ----------------------------------------------------------------------------------
k config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 || die "no kube context $KUBE_CONTEXT"
kn get ingress eddie >/dev/null || die "the ingress $NAMESPACE/eddie does not exist"
if (( freeze_check )); then
  [[ -e "$STATE/KILL" ]] || die "the Ember key is not frozen: run \`uv run python -m eddie.cli budget trip --reason \"submission freeze\"\` first (or pass --no-freeze-check)"
  [[ -f "$EXPORT" ]] || die "no final telemetry export: run \`uv run python -m eddie.cli budget export-trace\` first"
  last_call="$("$PY" -m eddie.cli budget status --json | jq -r '.recent[0].created_at // empty')"
  exported="$(jq -r '.generated_at // empty' "$EXPORT")"
  if [[ -n "$last_call" && "$exported" < "$last_call" ]]; then
    die "the telemetry export ($exported) is older than the last ledger call ($last_call): export again"
  fi
  say "frozen: kill switch present, telemetry exported $exported ($(jq -r '.totals.total_spend_usd' "$EXPORT") USD total)"
fi
if [[ -z "$youtube" ]]; then
  mode="$(jq -r '.mode // "local"' "$SIDECAR" 2>/dev/null || echo none)"
  cut="$(jq -r '.cut // "draft"' "$SIDECAR" 2>/dev/null || echo none)"
  [[ "$mode" == "youtube" ]] || warn "the film is still served from here (mode $mode): pass --youtube <url> to embed the public upload"
  [[ "$cut" == "final" ]] || warn "the film is still the $cut cut"
fi
say "reminder: the Lean archive ships no LICENSE file; the public site shows declaration statements and"
say "          names (never proof bodies), with attribution. Make sure you are comfortable with that."

printf 'Type UNLOCK to make https://%s public: ' "$HOST" >/dev/tty
answer=""
IFS= read -r answer </dev/tty || true
[[ "$answer" == "UNLOCK" ]] || die "not confirmed"

# --- the film first, so judges never see a draft ------------------------------------------------------
if [[ -n "$youtube" ]]; then
  "$ROOT/scripts/sync_demo_video.sh" --youtube "$youtube"
  "$ROOT/scripts/deploy_prod.sh" sync
fi

# --- the wall ---------------------------------------------------------------------------------------
kn annotate ingress eddie nginx.ingress.kubernetes.io/auth-type- nginx.ingress.kubernetes.io/auth-secret- \
  nginx.ingress.kubernetes.io/auth-realm- >/dev/null
sed -i 's/^AUTH=.*/AUTH=off/' "$ENV_FILE"
if grep -q '^PUBLIC_CACHE=' "$ENV_FILE"; then sed -i 's/^PUBLIC_CACHE=.*/PUBLIC_CACHE=1/' "$ENV_FILE"; else echo "PUBLIC_CACHE=1" >> "$ENV_FILE"; fi
"$ROOT/scripts/deploy_prod.sh" render >/dev/null
kn set env deploy/eddie-web EDDIE_PUBLIC_CACHE=1 >/dev/null
kn rollout status deploy/eddie-web --timeout=240s >/dev/null
say "unlocked: Basic Auth removed, public cache headers on"

sleep 5
fail=0
for path in / /healthz /api/demo-video/meta /api/chapters; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$HOST$path" || true)"
  say "  https://$HOST$path -> $code"
  [[ "$code" == 200 ]] || fail=1
done
(( fail == 0 )) || die "the site did not answer 200 everywhere; scripts/deploy_prod.sh status shows why"
say "the site is public. To put the wall back: scripts/set_prod_auth.sh --relock"
