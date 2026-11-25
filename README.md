# Eddie

**A 4-region autonomous proof decompiler running 100% on Meta Muse Ember, for less than $8 in total.**

On 2026-11-20 a formal Lean 4 proof of the Riemann Hypothesis was released: 175,187 `.lean` files and 17.4M lines, checked by the Lean kernel. Eddie does **not** re-compile it. It *decompiles* it into human sight, sound and intuition:

1. **A $0 local structural parser and index** (`eddie/ingest/`). It parses every file into imports, declaration signatures, docstrings and the active `open`/`variable` context. Proof bodies are stripped, but exact byte offsets are kept so any body can be loaded on demand. It also builds the import DAG (depth, degree, distance from `Main`/`Statements`, PageRank) and stores everything in SQLite FTS5.
2. **A 4-region "brain" on `muse-ember-1.0`:**
   - **R1 Salience Cartographer:** triage at minimal or low effort; at high effort it writes sandboxed numpy/mpmath/scipy/matplotlib scripts that render plots and `.wav` sonifications.
   - **R2 Multimodal Sensory Critic:** inspects the rendered PNGs, spectrograms and audio, and rejects defective artifacts.
   - **R3 Global Workspace Arbitrator:** checks at high, xhigh or max effort that the intuition faithfully reflects the formal hypotheses, with exact citations.
   - **R4 Nocturnal Consolidator:** synthesizes lemmas into a navigable 20-pillar Proof Atlas.
3. **An interactive Proof Explorer and Live Decompiler Studio** (React + Vite + Canvas + WebAudio) over a read-only FastAPI backend.

### Night 1, measured on the real archive

| | |
|---|---|
| Parsed | 175,187 modules, **1,065,486 declarations**, 753,569 import edges, in **~22 s** (18 cores); 0 `sorry`, 0 parser anomalies |
| Parser accuracy | **99.93%** of the hand-written, project-local names in the proof's dependency cone (from the published export) are recovered |
| Dead code | 24% of source declarations lie *outside* the proof cone, so Ember never needs to see them |
| The budget wall | One R1 triage pass over every signature would cost **~$15**. The $0 index has to decide what Ember ever sees. |

## The Metabolic Spend Governor

Contest rule: spend above **$10** on Ember disqualifies the entry, and the dashboard's alerts do not stop traffic. Ember also defaults to `effort="max"`, and above 200K input tokens the **whole** request costs 3×. So every call passes through a governor that sits physically in the request path (`eddie/llm/`):

| Lock | What it does |
|---|---|
| **Lifetime cap** ($8.00, code ceiling $9.00) | Committed spend plus the context-window worst case of every in-flight call must stay under the cap. The cap holds even if a token estimate were wrong. |
| **Run cap** ($0.10 per process) and **rolling 24 h cap** ($2.00) | An infinite-loop bug can lose at most 10 cents per run. Raising either cap needs a confirmation that is valid only on the date it names. |
| **Pre-flight reservation** | A provable upper-bound cost (UTF-8 bytes ≥ tokens) is reserved in SQLite before any socket opens: WAL, `BEGIN IMMEDIATE`, `synchronous=FULL`, CAS state transitions, concurrency capped across processes. It is reconciled to the exact `usage` afterwards. |
| **Mandatory explicit parameters** | `reasoning.effort`, `max_output_tokens` and the brain region are required. Calls are rejected if they omit them, use `effort="none"`, exceed region ceilings, use hidden-context features (`previous_response_id`, tools, `store: true`), or are estimated above 190K input tokens. All of it is re-checked at the wire. |

The rest of the governor:
- **Wire-level transport.** It only `POST`s to the one endpoint, using a single-use reservation bound to the sha256 of the exact body bytes. It is the only code that ever reads the API key, which never enters `os.environ`, a subprocess or a container.
- **Anchored ledger.** A copied, restored, deleted or second ledger is refused for live calls.
- **Strict usage parsing.** Invariant violations charge the full reservation and trip a manual-reset circuit breaker. There is also a kill switch.
- **Offline tests.** The test harness blocks every non-loopback socket and refuses to touch the real ledger, so tests cost $0 by construction.

`python -m eddie.cli budget export-trace` writes `eddie_ember_telemetry.json`: one row per billable call with `response_id`, region, effort, token counts, cost and cumulative spend, for proof of usage.

## Quickstart ($0)

```bash
uv sync
uv run pytest                                                   # ~300 tests, offline, $0
uv run python scripts/fetch_riemann_archive.py --sample         # synthetic mini tree, instant
uv run python -m eddie.cli index build --src data/sample/tree
uv run python -m eddie.cli index search zeta
uv run python -m eddie.cli llm ping --region R1.triage --effort minimal --max-output-tokens 16   # mock backend
uv run python -m eddie.cli budget status
```

Real archive (239 MB in 14 parts, every part sha256-pinned, resumable, safe streamed extraction):

```bash
uv run python scripts/fetch_riemann_archive.py --p0 --pdf
uv run python -m eddie.cli index build --src data/raw/extracted
uv run python -m eddie.cli index stats
uv run python -m eddie.cli index validate-spine data/raw/p0/spine.tsv.gz
```

UI: `make api` (FastAPI on :8765) plus `make web` (Vite on :5173), or `make compose-up` for the keyless, read-only container.

Backends (`EDDIE_BACKEND`):
- `mock` (default): deterministic, $0.
- `dgx`: an OpenAI-compatible local server, $0.
- `ember`: paid. It additionally needs `EDDIE_ALLOW_LIVE_EMBER=true`, an anchored ledger (`budget init`, run once interactively) and a mode-600 key file. It runs only from the host CLI, never in a container and never under pytest.

## Sandbox

`python -m eddie.cli sandbox run script.py` runs generated code in a fresh job directory:
- `python -I -B` with an environment built from scratch;
- rlimits, and a wall-clock kill of the whole process group;
- a PEP 578 audit hook that denies network, process creation, writes outside the job, and reads of secrets or ledgers under `$HOME`.

Every `.wav` gets a spectrogram PNG. Every image and audio file gets $0 QC (blank or NaN plots; silent, clipped or too-short audio), so the paid critic never sees an obviously broken artifact.

## Layout

| Path | What |
|---|---|
| `eddie/llm/` | Governor, ledger, pricing, estimator, usage parser, transport, gateway, adapters (mock/dgx/ember), telemetry |
| `eddie/ingest/` | Fetcher, lexer, parser, import graph, index build and queries, synthetic fixtures |
| `eddie/sandbox/` | Runner, child bootstrap, spectrogram/QC post-pass, artifact store |
| `eddie/api/` | Read-only FastAPI app |
| `web/` | React/Vite/Canvas UI |
| `k8s/` | Keyless PSA-`restricted` manifests (not applied) |
| `tests/` | The offline suite |

## Data and licensing

Eddie's code is MIT-licensed and was written from scratch for this hackathon. The Lean archive and companion PDFs are third-party material. They are downloaded locally into `data/`, never committed and never redistributed; `.gitignore` and a pre-commit hygiene check enforce this. Test fixtures are synthetic Lean written for this repository.
