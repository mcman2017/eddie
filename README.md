# Eddie

**A 4-region autonomous proof decompiler running 100% on Meta Muse Ember, for less than $8 in total.**

On 2026-11-20 a formal Lean 4 proof of the Riemann Hypothesis was released: 175,187 `.lean` files and 17.4M lines, checked by the Lean kernel. Eddie does **not** re-compile it. It *decompiles* it into human sight, sound and intuition:

1. **A $0 local structural parser and index** (`eddie/ingest/`). It parses every file into imports, declaration signatures, docstrings and the active `open`/`variable` context. Proof bodies are stripped, but exact byte offsets are kept so any body can be loaded on demand. It also builds the import DAG (depth, degree, distance from `Main`/`Statements`, PageRank) and stores everything in SQLite FTS5.
2. **A 4-region "brain" on `muse-ember-1.0`:**
   - **R1 Salience Cartographer:** triage at minimal or low effort; at high effort it writes sandboxed numpy/mpmath/scipy/matplotlib scripts that render plots and `.wav` sonifications.
   - **R2 Multimodal Sensory Critic:** inspects the rendered PNGs, spectrograms and audio, and rejects defective artifacts.
   - **R3 Global Workspace Arbitrator:** checks at high, xhigh or max effort that the intuition faithfully reflects the formal hypotheses, with exact citations.
   - **R4 Nocturnal Consolidator:** synthesizes lemmas into a navigable 20-pillar Proof Atlas.
3. **An interactive Proof Explorer and Live Decompiler Studio** (React + Vite + Three.js + Canvas + WebAudio) over a read-only FastAPI backend: a 3D critical strip that morphs between ζ and the proof's own wave, a dependency wheel, and a 20-chapter atlas.

### Night 1, measured on the real archive

| | |
|---|---|
| Parsed | 175,187 modules, **1,065,486 declarations**, 753,569 import edges, in **~22 s** (18 cores); 0 `sorry`, 0 parser anomalies |
| Parser accuracy | **99.93%** of the hand-written, project-local names in the proof's dependency cone (from the published export) are recovered |
| Dead code | 24% of source declarations lie *outside* the proof cone, so Ember never needs to see them |
| The budget wall | One R1 triage pass over every signature would cost **~$15**. The $0 index has to decide what Ember ever sees. |

### Night 1.5: the map and the plan, still $0

| | |
|---|---|
| Proof cone | 7.27M declaration references resolved Lean-style (namespaces, `open`, dot notation). Liveness comes from the kernel-checked export: **799,028 live** declarations (75.0%). Our own resolver agrees with it at precision 0.961, recall 0.925. |
| Structure | Personalized PageRank from `Main`, dominator tree and dominated mass, cross-pillar bridges, and one explainable importance score per declaration |
| Pillars | **20 proof pillars** found by splitting the live directory tree top-down; 17 are matched to the 20-step timeline in the companion remark |
| Budget plan | `plan-budget` costs every call with the governor's own estimator and prices. The worst case is **$5.81 of a $6.50 target**: R1 triage for 31,951 live declarations ($2.00), and the full R1 → R4 pipeline for 307 milestones covering all 20 pillars, every one connected to `Main` through its dominators ($3.82). The expected cost is $2.68, scheduled over 4 days under the $2 daily cap. |
| Explorer | A camera-first map of the whole proof, from `Main` down to single declarations, plus an inspector with visual and audio witnesses |

### Day 2: the critical strip, the proof's own wave and the atlas, still $0

| | |
|---|---|
| Chapter order | The 20 pillars are ordered by the proof's own direction: the exact minimum-feedback-arc order of the cross-pillar references (subset DP), grouped into four acts (6 foundations, 9 machinery, 4 bottleneck, Main). **86%** of 2.6M cross-pillar references point forward, against 65% before. Only **5 of the 20** remark steps are attached to a chapter, the ones matched without ambiguity; the old 17 word-overlap guesses are gone. |
| The ζ terrain | `build-landscape` evaluates ζ(σ + it) on an 81×481 grid of the critical strip with mpmath and checks it against the functional equation (residual 1.5e-12). The 13 zeros up to t = 60 are computed separately to 15 digits. |
| The proof wave | Every module gets a Gödel-coded structural signature (depth, fan-in, fan-out, its own pillar and the pillars it imports, as prime exponents). 175,187 modules fall into **16,743 exact classes**, and Z(s) = Σ w·e^(−s·ℓ) is evaluated once for the 799,028 live declarations and once for the 266,458 dead ones, in 8 harmonic bands. The whole build takes about 30 s. |
| What it shows | ζ keeps all 13 of its zeros on σ = 1/2. The live wave has **20 zeros scattered across the strip** (11 to 20 across 19 other encodings; 15.8 ± 2.9 for random surrogates). Dead code sits in fewer effective classes (**1,415 against 2,971**; 2,260 for a size-matched live sample), so its wave is the more concentrated one. The zeros of ζ sit in dips of the wave no more than chance allows (p = 0.76 live, 1.00 dead). |
| Atlas | `seed-demo` writes one structural stub per chapter (badged `$0 Structural Stub — Ready for R4 Muse Ember`); `/api/chapters` serves them with net flows, bottleneck theorems and witnesses. A stub can never overwrite a chapter a model wrote. |
| 3D view | Three.js: morph between the two surfaces, harmonic band slider, Live / Dead / Compare, camera presets and a tour, a WebAudio chord from the zeros or the wave's peaks. `scripts/verify_strip.mjs` drives it in headless Chrome. |

**What the wave is and is not.** It is an empirical, name-blind transform of the dependency graph that shares the basis functions of a Dirichlet series. It is not a number-theoretic object and proves nothing about ζ: it has no functional equation, no Euler product and no distinguished line, and σ = 1/2 is drawn only to share ζ's frame. Where an individual zero or dip sits depends on conventions (which prime codes which feature), so `findings.json` marks every statement as exact, stable across an ensemble of encodings, or specific to this numbering. Bottlenecks of the proof are measured by dominators (one theorem gates 84.6% of the live cone), not by the wave. The chapter beacons on the strip are a layout, not a computation.

### Day 2.5: a light studio, a visual tour and the first-call checklist, still $0

| | |
|---|---|
| One light theme | The explorer is ink on gallery paper. Chrome is neutral and hue is spent only on data: the four acts (cobalt, jade, magenta-violet, ember; checked for colour-blind separation on every surface), the height of the two surfaces, and the critical line. A status is a shape (check badge, dot, ring, dashed ring), never a hue. Every value lives in `web/src/palette.json`, mirrored for the Python plots in `eddie/sandbox/style.py`; a test keeps the two equal. |
| The strip | A painted relief model: height is colour (two multi-hue ramps baked in OKLab), contours and phase lines are ink, the critical line is a gold core in a crimson casing, each zero of ζ is ringed and badged, and nothing is additive (a glow adds up to white on paper). Dead code is the same wave drained to grey at its true height; Compare lays it over the live surface as a grid. |
| Visual tour | **Play Visual Tour** runs one continuous 40 s move through five shots (zeros on the line, the 20-chapter funnel, the morph into the proof wave, dead code against the live proof, the summit at `Main.lean`). The camera follows a spline that is at rest only at the two ends, the scene changes on the same clock, and the tour is identical at any frame rate. Each shot is also a pill and a key (1 to 5). |
| Capture layout | `#cinema` keeps the top bar, the canvas, the legend and one lower-third caption, draws the canvas at the display's own pixel ratio, and slides the chapter drawer in over the canvas when the tour opens the last chapter. |
| The gate | The proof's real bottleneck is measured on the dependency graph: `allChi` gates **84.6%** of the live proof. It is tagged on chapter 20's beacon and named in the tour's last caption. The findings card leads with one sentence generated from the numbers; it never calls a dip of the wave a bottleneck. |
| Plots | Sandboxed scripts get a house plot style through a packaged `MATPLOTLIBRC`, so generated code spends nothing on styling. The seed witnesses are redrawn on white, and a rejected render is shown above the one that replaced it, each under its verdict. |
| A fix | Outside frozen screenshots the 3D view's frame time was always zero, so Live / Dead / Compare never reshaped the surface on a running page. Fixed, with a live check that fails without the fix. |

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
uv run pytest                                                   # 428 tests, offline, $0
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

Map, plan and explorer (all $0; the cone needs the real index above and takes about a minute):

```bash
make cone       # declaration graph, live proof cone, pillars, chapter order -> data/index/eddie_cone.sqlite
make landscape  # zeta terrain, proof wave, findings -> data/index/landscape/ (about 30 s, no model calls)
make plan       # exact tiered Ember plan inside $6.50 -> data/index/work_plan.json (no model calls)
make seed       # sandbox-rendered demo witnesses and 20 chapter stubs, labeled seed (never Ember output)
make dev        # API on 127.0.0.1:8765 + Vite on 127.0.0.1:5173
node scripts/verify_strip.mjs   # headless Chrome check of the 3D view (after make web-build and make api)
```

Both servers bind to loopback only. To view them from another machine, forward the ports over SSH (`ssh -N -L 5173:127.0.0.1:5173 -L 8765:127.0.0.1:8765 <host>`) and open `http://localhost:5173`. Deep links work and combine: `#view=wheel`, `#mode=wave`, `#pop=dead|compare`, `#chapter=<slug>`, `#shot=1..5`, `#cinema`, `#decl=<id>`, `#node=<id>`. In the 3D view, keys 1 to 5 fly to the five shots, 0 returns to the overview, T plays or stops the visual tour, Escape stops it and O toggles auto-orbit. For a recording, open `#cinema` and press T. `make compose-up` runs the keyless, read-only API container instead.

Backends (`EDDIE_BACKEND`):
- `mock` (default): deterministic, $0.
- `dgx`: an OpenAI-compatible local server, $0.
- `ember`: paid. It additionally needs `EDDIE_ALLOW_LIVE_EMBER=true`, an anchored ledger (`budget init`, run once interactively) and a mode-600 key file. It runs only from the host CLI, never in a container and never under pytest.

## First live call

Nothing live exists until you run this, once, by hand, on the host. D is what Meta's dashboard shows for this key since Nov 23.

1. `git status` is clean and `uv run pytest -q` is green.
2. Put the key in a mode-600 file, never in the repo or an env file: `install -d -m 700 ~/.secrets/eddie`, then `(umask 077; cat > ~/.secrets/eddie/meta_api_key)`, paste, Ctrl-D.
3. `uv run python -m eddie.cli budget init --dashboard-usd D`, and type `INIT`.
4. `uv run python -m eddie.cli budget status`: anchor `ok`, breaker `closed`, kill switch `absent`.
5. Rehearse at $0: `uv run python -m eddie.cli llm ping --region R1.triage --effort minimal --max-output-tokens 16` prints `backend       mock`.
6. One live call: `EDDIE_BACKEND=ember EDDIE_ALLOW_LIVE_EMBER=true uv run python -m eddie.cli llm ping --region R1.triage --effort minimal --max-output-tokens 16`. It reserves about $0.00012 against the run and daily caps and $0.3146 (the worst case) against the lifetime gate while in flight, then settles near $0.00001. The flags must be in the process environment: nothing loads `.env`.
7. `budget status` again: one settled call, `anomalous: 0`, breaker `closed`. If the breaker is open, stop and read the reason before any `reset-breaker`.
8. `make dev`: the top pill reads about `$0.00001 of $8.00 cap, 1 live call`.
9. `uv run python -m eddie.cli budget export-trace`, then compare with the dashboard once it updates. `budget trip --reason "..."` is the manual kill switch.

## Sandbox

`python -m eddie.cli sandbox run script.py` runs generated code in a fresh job directory:
- `python -I -B` with an environment built from scratch;
- rlimits, and a wall-clock kill of the whole process group;
- a PEP 578 audit hook that denies network, process creation, writes outside the job, and reads of secrets or ledgers under `$HOME`.

Every script gets the house plot style through `MATPLOTLIBRC` (`eddie/sandbox/eddie.matplotlibrc`, rendered from `eddie/sandbox/style.py`): white figures, ink text and the validated colour cycle, with no styling code in the script. Every `.wav` gets a spectrogram PNG. Every image and audio file gets $0 QC (blank or NaN plots; silent, clipped or too-short audio), so the paid critic never sees an obviously broken artifact.

## Layout

| Path | What |
|---|---|
| `eddie/llm/` | Governor, ledger, pricing, estimator, usage parser, transport, gateway, adapters (mock/dgx/ember), telemetry |
| `eddie/ingest/` | Fetcher, lexer, parser, import graph, index build and queries, declaration references, proof cone, zoom tree, synthetic fixtures |
| `eddie/planner/` | Proof pillars and their chapter order, the atlas facts, the zeta/proof-wave landscape, the exact tiered budget planner |
| `eddie/state/` | The mutable artifacts database (work queue, triage, intuitions, witnesses, reviews) |
| `eddie/sandbox/` | Runner, child bootstrap, spectrogram/QC post-pass, artifact store |
| `eddie/demo/` | `seed-demo` and its witness scripts |
| `eddie/api/` | Read-only FastAPI app and explorer API |
| `web/` | React/Vite explorer: the Three.js critical strip, the dependency wheel, the chapter guide, inspector, witness and audio players |
| `k8s/` | Keyless PSA-`restricted` manifests (not applied) |
| `tests/` | The offline suite |

## Data and licensing

Eddie's code is MIT-licensed and was written from scratch for this hackathon. The Lean archive and companion PDFs are third-party material. They are downloaded locally into `data/`, never committed and never redistributed; `.gitignore` and a pre-commit hygiene check enforce this. Test fixtures are synthetic Lean written for this repository.
