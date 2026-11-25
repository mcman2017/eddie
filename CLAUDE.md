# Eddie: project rules

Eddie decompiles a formally verified Lean 4 proof archive into human sight, sound and intuition. Its 4 "brain regions" run on Meta's `muse-ember-1.0`, and everything else is $0 local work. This is a hackathon entry: **total Ember spend above $10 disqualifies it.** These rules exist to keep it far below that line.

## Never
- Copy, import or reference code from any other project. Every line here is written fresh (clean-room).
- Commit `*.lean`, archive parts (`*.tar*`), anything under `data/`, `*.sqlite*`, `.env*` (except `.env.example`), `initial-prompt.md` or `CLAUDE.local.md`. The pre-commit hook (`.githooks/`) and `scripts/check_repo_hygiene.py` enforce this. Test fixtures are synthetic Lean written as Python strings, never archive text.
- Call Ember from tests. Tests are 100% offline: `tests/conftest.py` blocks every non-loopback socket and refuses to open the real ledger, anchor or key. Use `httpx.MockTransport`.
- Give a container (compose, k8s) the Ember key or the ledger. Containers serve precomputed data read-only and mount only `~/.local/share/eddie/public/`.
- `cp`, move or delete the ledger (`~/.local/share/eddie/ledger.sqlite`) or its anchor (`~/.config/eddie/ledger-anchor.json`). A copied or recreated ledger is refused for live calls by design. Back up only with SQLite's online backup API.
- Construct an httpx client for `api.meta.ai` anywhere but `eddie/llm/transport.py`, or read the key anywhere else.
- Use Streamlit, or build a passive dashboard or chatbot.

## Spend governor invariants (`eddie/llm/`)
- Every Ember call: explicit `reasoning.effort` (never `none`, never omitted), explicit `max_output_tokens` (≥ 16, ≤ the region ceiling), explicit `brain_region`, and `store: false`.
- Reserve before I/O, settle after. The lifetime gate uses the context-window worst case `W(m)`, so it holds even if an estimate is wrong.
- The input estimate is an upper bound (UTF-8 bytes). An estimate over 190,000 is refused, because above 200K input the whole request costs 3×.
- Unknown modalities (audio, PDF, URLs, file ids) are refused unless the request explicitly opts into a worst-case reservation.
- Breaker trips are manual-reset only (`python -m eddie.cli budget reset-breaker`).
- `budget status` and `budget export-trace` are the source of truth for spend. Proof of usage is `exports/eddie_ember_telemetry.json`.

## Commands
- `uv sync`, then `uv run pytest` (offline, fast) and `uv run ruff check .`
- `uv run python scripts/fetch_riemann_archive.py --sample` (synthetic tree) or `--p0 --pdf` (real archive into `data/raw/`)
- `uv run python -m eddie.cli index build --src data/raw/extracted`, then `index stats` / `search` / `show`
- `uv run python -m eddie.cli budget status|init|export-trace|trip|reset-breaker`
- `make api`, then `make web` (API on :8765, Vite on :5173)

## Layout
`eddie/llm/` holds the governor and gateway. `eddie/ingest/` holds the Lean parser, DAG and index. `eddie/sandbox/` holds the script runner. `eddie/api/` is the read-only FastAPI app. `web/` is the React/Vite/Canvas UI. `scripts/` holds the fetcher and hygiene checks. `k8s/` holds manifests (keyless, read-only).
