# data/ (git-ignored except this file)

Rebuildable local state. Nothing in here is ever committed.

| Path | What |
|---|---|
| `raw/parts/` | `lean.tar.gz.part01..14`, sha256-verified |
| `raw/extracted/` | The extracted Lean project (175,187 `.lean` files) |
| `raw/p0/` | `spine.tsv.gz`, `stats.json`, `report.md` from the mirror |
| `raw/pdfs/` | Companion PDFs (`remark.pdf`) |
| `sample/` | Synthetic mini Lean tree (`fetch_riemann_archive.py --sample`) |
| `index/eddie_index.sqlite` | Structural index (FTS5 plus import graph) |
| `shadow.sqlite` | $0 mock/dgx call log with the would-be Ember cost |
| `artifacts/` | Content-addressed sandbox outputs (PNG, WAV, spectrograms, QC) |

The **spend ledger is not here.** It lives in `~/.local/share/eddie/` so that no clean of this directory can reset it.
