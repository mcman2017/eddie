#!/usr/bin/env bash
# Put "A Fistful of Tokens" where the explorer serves it from (data/media/, git-ignored).
#
#   scripts/sync_demo_video.sh                    the film project's final cut if it exists, else its 720p draft
#   scripts/sync_demo_video.sh --src FILE.mp4     a specific file
#   scripts/sync_demo_video.sh --youtube URL|ID   embed the public YouTube upload; the MP4 stops being served
#
# The film project (~/eddie-demo) is only ever read. `scripts/deploy_prod.sh sync` then ships the
# result to the cluster; the API reads the sidecar per request, so no pod restarts.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/.venv/bin/python" -m eddie.cli media sync "$@"
