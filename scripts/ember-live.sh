#!/usr/bin/env bash
# Run the operator CLI against live Ember with the key taken from the Kubernetes Secret that
# scripts/ember-key.sh stores. The key goes from the API server into this process's memory and
# on to the CLI over an inherited pipe (fd 3, named by EDDIE_KEY_FD). It is never written to a
# file, never put on a command line and never put in an environment variable: /proc/<pid>/environ
# keeps a process's original environment for its whole life, readable by any process of the same
# user, sandboxed children included. eddie/llm/transport.py reads the pipe once and closes it.
#
#   scripts/ember-live.sh llm ping --region R1.triage --effort minimal --max-output-tokens 16
#
# Everything after the script name is passed to `python -m eddie.cli`. This sets the two live
# flags for that one process only. Every guard still applies: the anchored ledger, the caps,
# the breaker, the kill switch, and the refusal to run inside a container.

set -euo pipefail
case "$-" in *x*) echo "refusing to run with shell tracing on (set -x would print the key)" >&2; exit 2 ;; esac
ulimit -c 0 2>/dev/null || true

NS="${EDDIE_KEY_NAMESPACE:-eddie-operator}"
NAME="${EDDIE_KEY_SECRET:-ember-api-key}"
FIELD="meta_api_key"
KUBECTL="${KUBECTL:-kubectl}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

[[ $# -gt 0 ]] || { echo "usage: scripts/ember-live.sh <eddie.cli arguments>, e.g. llm ping --region R1.triage --effort minimal --max-output-tokens 16" >&2; exit 2; }
[[ -x "$PYTHON" ]] || { echo "error: $PYTHON is missing: run uv sync first" >&2; exit 1; }

key="$("$KUBECTL" -n "$NS" get secret "$NAME" -o "jsonpath={.data.$FIELD}" | base64 -d)" \
  || { echo "error: could not read $NS/$NAME: run scripts/ember-key.sh first" >&2; exit 1; }
[[ -n "$key" ]] || { echo "error: $NS/$NAME is empty: run scripts/ember-key.sh" >&2; exit 1; }

# printf is a builtin, so the key is in no process's arguments; the pipe holds it until the CLI
# reads it. exec replaces this shell, so no copy of the key outlives the CLI.
exec 3< <(printf '%s' "$key")
key=""
EDDIE_KEY_FD=3 EDDIE_BACKEND=ember EDDIE_ALLOW_LIVE_EMBER=true exec "$PYTHON" -m eddie.cli "$@"
