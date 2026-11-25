#!/usr/bin/env python3
"""Fetch the 14-part Lean 4 RH archive + companions into git-ignored data/raw/.

    uv run python scripts/fetch_riemann_archive.py --sample        # synthetic tree, instant
    uv run python scripts/fetch_riemann_archive.py --p0 --pdf      # real archive (239 MB)
    uv run python scripts/fetch_riemann_archive.py --verify-only

All logic lives in eddie.ingest.fetch (importable and unit-tested).
"""

from eddie.ingest.fetch import main

if __name__ == "__main__":
    raise SystemExit(main())
