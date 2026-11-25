# Eddie

**A 4-region autonomous proof decompiler running 100% on Meta Muse Ember, for less than $8 in total.**

On 2026-11-20 a formal Lean 4 proof of the Riemann Hypothesis was released: 175,187 `.lean` files and ~17.4M lines, checked by the Lean kernel. Eddie does **not** re-compile it. It *decompiles* it into human sight, sound and intuition:

1. **$0 local structural parser.** It parses every `.lean` file to extract imports, `theorem`/`lemma`/`def` signatures, docstrings and byte offsets. It builds the import DAG with depth, degree, distance from `Main.lean`/`Statements.lean` and PageRank, and stores everything in a SQLite FTS5 + graph index. 1.21M project declarations is far too many to send to any model, so this index decides what is worth Ember's attention.
2. **A 4-region "brain" on `muse-ember-1.0`:**
   - **R1 Salience Cartographer:** triage at minimal effort; at high effort it writes sandboxed numpy/mpmath/scipy/matplotlib scripts that render plots and `.wav` sonifications.
   - **R2 Multimodal Sensory Critic:** inspects the rendered PNGs, spectrograms and audio, and rejects defective artifacts.
   - **R3 Global Workspace Arbitrator:** checks at xhigh/max effort that the intuition faithfully reflects the formal hypotheses, with exact citations.
   - **R4 Nocturnal Consolidator:** synthesizes lemmas into a navigable 20-pillar Proof Atlas.
3. **An interactive Proof Explorer and Live Decompiler Studio** (React + Vite + Canvas + WebAudio) over a FastAPI backend.

## The Metabolic Spend Governor

Contest rule: spend above **$10** on Ember disqualifies the entry, and Meta's dashboard alerts do not stop traffic. Ember also defaults to `effort="max"`, and above 200K input tokens the **whole** request costs 3×. So every call passes a governor that is physically in the request path:

| Lock | What it does |
|---|---|
| **Lifetime cap** ($8.00, code ceiling $9.00) | Refuses to reserve once committed spend + the worst case of every in-flight call would cross the cap. The worst case is bounded by the context window, so the cap holds even if a token estimate were wrong. |
| **Run cap** ($0.10 per process) and **rolling 24 h cap** ($2.00) | An infinite-loop bug can lose at most 10 cents per run. Raising either cap needs a confirmation that is valid only on the date it names. |
| **Pre-flight reservation** | An upper-bound cost is reserved in SQLite (WAL, `BEGIN IMMEDIATE`, `synchronous=FULL`) before any socket opens, and reconciled to the exact `usage` afterwards. Concurrency is capped across processes. |
| **Mandatory explicit parameters** | `reasoning.effort`, `max_output_tokens` and the brain region are required. Calls are rejected if they omit them, use `effort="none"`, exceed region ceilings, use hidden-context features (`previous_response_id`, tools, `store: true`), or are estimated above 190K input tokens. All of it is re-checked at the wire. |

Other guards:
- One canonical ledger, bound to an anchor file. A copied, deleted or second ledger is refused.
- A manual-reset circuit breaker and a kill switch file.
- Key custody: only the transport ever reads the key.
- Tests can never reach the network.

Every billable call is exported to `eddie_ember_telemetry.json` for proof of usage.

## Quickstart ($0)

```bash
uv sync
uv run pytest                                        # offline, $0
uv run python scripts/fetch_riemann_archive.py --sample
uv run python -m eddie.cli index build --src data/sample/tree
uv run python -m eddie.cli index search zeta
uv run python -m eddie.cli llm ping --region R1.triage --effort minimal --max-output-tokens 16   # mock backend
uv run python -m eddie.cli budget status
```

Real archive (239 MB, sha256-verified, resumable):

```bash
uv run python scripts/fetch_riemann_archive.py --p0 --pdf
uv run python -m eddie.cli index build --src data/raw/extracted
```

Backends: `EDDIE_BACKEND=mock` (default, deterministic, $0), `dgx` (a local OpenAI-compatible server, $0), or `ember` (paid). Ember additionally needs `EDDIE_ALLOW_LIVE_EMBER=true`, an anchored ledger (`budget init`) and a key file.

## Data and licensing

Eddie's code is MIT-licensed. The Lean archive and companion PDFs are third-party material: they are downloaded locally, never committed, and never redistributed (`.gitignore` plus a pre-commit hygiene check).
