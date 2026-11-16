"""Rebuild every vector from the stored chunk text with the current EMBEDDING_* settings.

  uv run python -m scripts.reembed --dry-run   # show what would happen
  uv run python -m scripts.reembed             # re-embed (throttled, resumable)
  uv run python -m scripts.reembed --yes       # also allow changing the vector size

Exit codes: 0 done, 1 refused or failed, 2 stopped by a quota, billing or rate-limit problem.
"""

import argparse
import sys

from app.config import get_settings
from app.db import connect
from app.ingest.embedder import (
    EmbeddingAccountError,
    EmbeddingError,
    EmbeddingNotConfigured,
    EmbeddingRateLimited,
    build_embedder,
)
from app.services.reembed import ReembedRefused, plan, reembed
from app.startup import prepare_database
from scripts.ingest_folder import print_progress, print_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-embed all stored chunks.")
    parser.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    parser.add_argument("--yes", action="store_true", help="allow changing the vector size")
    args = parser.parse_args(argv)

    settings = get_settings()
    prepare_database(settings, check=False)  # create/upgrade tables; skip the mismatch check
    with connect(settings.database_url) as conn:
        info = plan(conn, settings.embedding_model, settings.embedding_dimensions)
    print(
        f"config: {settings.embedding_model} / {settings.embedding_dimensions} dimensions; "
        f"database built with: {info['built_with']}; column size: {info['column_dimensions']}"
    )
    print(
        f"chunks: {info['chunks']}; to re-embed: {info['to_embed']}; "
        f"resizes column: {info['resizes_column']}"
    )
    if info["in_progress_target"]:
        print(f"an unfinished re-embed to '{info['in_progress_target']}' will be continued")
    if args.dry_run:
        return 0

    try:
        embedder = build_embedder(settings)
        summary = reembed(
            settings.database_url, embedder, allow_resize=args.yes, progress=print_progress
        )
    except EmbeddingNotConfigured as exc:
        print(f"STOP: {exc}")
        return 1
    except ReembedRefused as exc:
        print(f"REFUSED: {exc}")
        return 1
    except (EmbeddingAccountError, EmbeddingRateLimited) as exc:
        print(f"STOP: {exc}")
        print("Progress is saved. Tell the owner, then run the same command to resume.")
        return 2
    except EmbeddingError as exc:
        print(f"FAILED: {exc}")
        return 1
    print_summary(summary)
    print("re-embed complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
