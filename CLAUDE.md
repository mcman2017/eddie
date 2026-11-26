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
- `make cone` (cone build, ~1 min), `make landscape` (build-landscape, ~30 s), `make plan` (plan-budget), `make seed` (seed-demo), then `make dev` (API and Vite on 127.0.0.1:8765 and :5173)
- Visual checks: headless `google-chrome` against 127.0.0.1 only (`node scripts/verify_strip.mjs` after `make web-build` and `make api`; screenshots land in `data/verify/`); seed rows are `source='seed'` and must never be shown or reported as Ember output.
- The cone keeps two pillar orders: `ord` (zoom tree and budget planner; pillar 0 first) and `flow_ord` (chapter order). Change `flow_ord` only; changing `ord` moves the budget plan.
- Claims about the proof wave follow `findings.json`: say "stable" only for what holds across the encoding ensemble, never call a dip a bottleneck, and never present the wave as a number-theoretic object.
- One theme, light. Colours come from `web/src/palette.json` (through `web/src/theme.ts`) and its Python mirror `eddie/sandbox/style.py`; `tests/test_palette_parity.py` keeps them equal, so change both. Hue is for data only (acts, height ramps, the critical line); a status is a shape with a word. Re-run the palette validator before changing an act colour: cobalt and magenta-violet stay apart only by lightness.
- Nothing in the 3D view is additive (a glow adds up to white on paper): mix lines toward ink and give every mark a white casing.
- The visual tour lives in `web/src/strip/tour.ts` (pure, auditable without a GPU). Check a frame with `#freeze=12.5&tour=<seconds>`; the capture layout is `#cinema`. The headless check must keep one live (unfrozen) case per animated control: a frozen frame assigns state directly and hides clock bugs.
- The takeaway sentence and the gate record are generated in `eddie/planner/zeta_landscape.py`; reword them there, then `make landscape`.
- `run-ember`: rehearse at $0 first (`make rehearse`, `--dry-run`); live only through `scripts/ember-live.sh` (the key arrives on fd 3; a key in `META_API_KEY` is refused). Every call goes through a call slot (`eddie/pipeline/orchestrator.py`): never call the gateway around it, or a resume can pay twice.
- Model-written scripts run only in the container sandbox (`make sandbox-image`; `SandboxRunner(isolation="docker")`). The in-process sandbox is for our own scripts (`seed-demo`, tests).
- Every `source='ember'` row needs its ledger `call_id` and `response_id`; `run-ember audit` checks them against the ledger.
- The narration (`web/src/strip/narration.json`) uses the tour's shot windows; `tests/test_narration.py` fails when `CUES` change until `make narration` re-renders it. Name colours as `palette.json` draws them.
- `#cinema` must look exactly as before: the voice switch and the film card are never rendered there.
- Deploy: real host, registry and context live only in git-ignored `k8s/overlays/prod/deploy.env`; tracked files keep placeholders (hygiene blocks the real names).

## Layout
`eddie/llm/` holds the governor and gateway. `eddie/ingest/` holds the Lean parser, DAG, index, reference graph, cone and zoom tree. `eddie/planner/` holds pillars and chapter flow, the atlas facts (`chapters.py`), the landscape precomputation (`zeta_landscape.py`) and the budget planner. `eddie/state/` is the mutable artifacts DB. `eddie/sandbox/` holds the script runner and `eddie/demo/` the seed witnesses. `eddie/api/` is the read-only FastAPI app. `eddie/pipeline/` is `run-ember`. `web/` is the React/Vite explorer (`src/strip/` is the Three.js critical strip, `mapEngine.ts` the dependency wheel, `FilmCard.tsx` the film). `scripts/` holds the fetcher, hygiene checks, the narration build and the deploy scripts. `k8s/base/` holds manifests (keyless, read-only); `k8s/overlays/prod/` is git-ignored.
