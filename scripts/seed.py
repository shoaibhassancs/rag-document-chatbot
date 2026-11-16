"""Build, load or check the pre-embedded seed file for the sample documents.

  uv run python -m scripts.seed build   # export ready documents + vectors from the database
  uv run python -m scripts.seed load    # load the seed into an empty (or existing) database
  uv run python -m scripts.seed check   # is the seed current for data/sample_docs?

`build` makes no Voyage calls (it reads vectors that are already in the database).
OPTIONAL and LOCAL ONLY: the seed file is never committed (`data/seed/` stays in .gitignore
permanently, see DECISIONS.md D-027). A full ingestion takes only about 2 minutes, so a fresh clone
simply runs `scripts.ingest_folder`. The seed tool is a local convenience.
"""

import argparse
import sys
from pathlib import Path

from app.config import get_settings
from app.db import connect
from app.services.seed import SeedError, build_seed, load_seed, read_seed, stale_report, write_seed
from app.startup import prepare_database

DEFAULT_SEED = Path("data/seed/sample_docs.seed.json.gz")
DEFAULT_DOCS = Path("data/sample_docs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-embedded seed file for the sample documents.")
    parser.add_argument("action", choices=["build", "load", "check"])
    parser.add_argument("--file", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS, help="folder used by `check`")
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        if args.action == "build":
            prepare_database(settings)
            with connect(settings.database_url) as conn:
                seed = build_seed(conn, settings)
            size = write_seed(seed, args.file)
            chunks = sum(len(d["chunks"]) for d in seed["documents"])
            print(
                f"wrote {args.file}: {len(seed['documents'])} documents, {chunks} chunks, "
                f"{size:,} bytes"
            )
            return 0
        seed = read_seed(args.file)
        if args.action == "load":
            prepare_database(settings)
            with connect(settings.database_url) as conn:
                counts = load_seed(conn, seed, settings)
                conn.commit()
            print(f"seed loaded: {counts}")
            return 0
        report = stale_report(seed, args.docs, settings)
        stale = any(report.values())
        for key, names in report.items():
            for name in names:
                print(f"{key}: {name}")
        print("seed is STALE: rebuild it" if stale else "seed is current")
        return 1 if stale else 0
    except SeedError as exc:
        print(f"SEED ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
