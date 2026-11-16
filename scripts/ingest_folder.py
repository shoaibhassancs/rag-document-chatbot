"""Ingest a folder (and optionally URLs): load, dedupe, chunk, embed (throttled) and store.

  uv run python -m scripts.ingest_folder data/sample_docs --dry-run   # estimate only, no DB/Voyage
  uv run python -m scripts.ingest_folder data/sample_docs             # real run, safe to repeat
  uv run python -m scripts.ingest_folder --url https://example.com/page

Exit codes: 0 all fine, 1 some files failed or were rejected, 2 stopped by a quota, billing or
rate-limit problem (progress is saved; tell the owner, then run again to resume).
"""

import argparse
import sys
from pathlib import Path

from app.config import Settings, get_settings
from app.db import connect
from app.ingest.embedder import (
    EmbeddingAccountError,
    EmbeddingError,
    EmbeddingNotConfigured,
    EmbeddingRateLimited,
    VoyageEmbedder,
    build_embedder,
)
from app.ingest.loaders import SUPPORTED_EXTENSIONS, LoadedDocument, LoadError, load_bytes, load_url
from app.services.ingestion import (
    EmbedSummary,
    Progress,
    embed_pending,
    estimate_run,
    register_document,
)
from app.startup import prepare_database


def collect_files(folder: Path) -> list[Path]:
    """Supported files below `folder`, skipping hidden files and folders. Sorted by folder, then
    file stem, then extension, so "faq.md" comes before "faq-printed-copy.md"."""
    files = [
        p
        for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_EXTENSIONS
        and not any(part.startswith(".") for part in p.relative_to(folder).parts)
    ]
    return sorted(files, key=lambda p: (str(p.parent), p.stem.lower(), p.suffix.lower()))


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def load_all(
    folder: Path | None, urls: list[str], settings: Settings
) -> tuple[list[LoadedDocument], list[tuple[str, str]]]:
    docs: list[LoadedDocument] = []
    failures: list[tuple[str, str]] = []
    if folder is not None:
        for path in collect_files(folder):
            name = str(path.relative_to(folder)) if path.parent != folder else path.name
            try:
                docs.append(load_bytes(name, path.read_bytes()))
            except LoadError as exc:
                failures.append((name, f"{exc.code}: {exc.message}"))
    for url in urls:
        try:
            docs.append(
                load_url(
                    url,
                    allow_private=settings.allow_private_urls,
                    timeout=settings.url_timeout_seconds,
                    max_bytes=settings.url_max_bytes,
                )
            )
        except LoadError as exc:
            failures.append((url, f"{exc.code}: {exc.message}"))
    return docs, failures


def print_progress(p: Progress) -> None:
    print(
        f"batch {p.batch}/{p.batches}  {p.batch_estimated_tokens:,} est tokens  "
        f"{p.batch_chunks} chunks | embedded {p.embedded}/{p.total} | "
        f"elapsed {format_duration(p.elapsed_seconds)}",
        flush=True,
    )


def dry_run(docs: list[LoadedDocument], failures: list[tuple[str, str]], settings: Settings) -> int:
    est = estimate_run(
        docs,
        chunk_size_tokens=settings.chunk_size_tokens,
        chunk_overlap_tokens=settings.chunk_overlap_tokens,
        safety_factor=VoyageEmbedder.FIRST_BATCH_SAFETY,
        max_batch_tokens=settings.voyage_max_batch_tokens,
        requests_per_minute=settings.voyage_requests_per_minute,
        tokens_per_minute=settings.voyage_tokens_per_minute,
    )
    print("DRY RUN: nothing is sent to Voyage and the database is not touched.")
    print(f"documents to ingest : {est.documents}")
    for name, original in est.duplicates:
        print(f"  duplicate skipped : {name} (same text as {original})")
    for name, reason in failures:
        print(f"  cannot load       : {name} ({reason})")
    print(f"chunks              : {est.chunks} ({est.unique_chunks} unique texts to embed)")
    safety = VoyageEmbedder.FIRST_BATCH_SAFETY
    print(f"estimated tokens    : {est.estimated_tokens:,} (chars/4 x {safety} safety factor)")
    print(f"batches (requests)  : {est.batches}")
    print(
        f"estimated time      : {format_duration(est.estimated_seconds)} at "
        f"{settings.voyage_requests_per_minute} req/min and "
        f"{settings.voyage_tokens_per_minute:,} tokens/min"
    )
    print("Ignores chunks that are already embedded in the database, so a repeat run costs less.")
    return 1 if failures else 0


def print_summary(summary: EmbedSummary) -> None:
    print("--- embedding summary ---")
    print(f"chunks embedded (new vectors) : {summary.chunks_embedded}")
    print(f"chunks reused (same text)     : {summary.chunks_reused}")
    print(f"requests to Voyage            : {summary.requests}")
    print(
        f"tokens: estimated / actual    : {summary.estimated_tokens:,} / {summary.actual_tokens:,}"
    )
    print(f"elapsed                       : {format_duration(summary.elapsed_seconds)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest documents into the knowledge base.")
    parser.add_argument("folder", nargs="?", type=Path, help="folder to scan recursively")
    parser.add_argument("--url", action="append", default=[], help="one web page (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="estimate only; no DB, no Voyage")
    args = parser.parse_args(argv)
    if args.folder is None and not args.url:
        parser.error("give a folder, one or more --url values, or both")
    if args.folder is not None and not args.folder.is_dir():
        parser.error(f"not a folder: {args.folder}")

    settings = get_settings()
    docs, failures = load_all(args.folder, args.url, settings)
    if args.dry_run:
        return dry_run(docs, failures, settings)

    try:
        embedder = build_embedder(settings)
    except EmbeddingNotConfigured as exc:
        print(f"STOP: {exc}")
        return 1
    prepare_database(settings)

    counts: dict[str, int] = {}
    with connect(settings.database_url) as conn:
        for number, doc in enumerate(docs, start=1):
            result = register_document(
                conn,
                doc,
                chunk_size_tokens=settings.chunk_size_tokens,
                chunk_overlap_tokens=settings.chunk_overlap_tokens,
                embedding_model=settings.embedding_model,
            )
            conn.commit()
            counts[result.status] = counts.get(result.status, 0) + 1
            extra = f" ({result.action}, {result.chunks} chunks)" if result.action else ""
            if result.duplicate_of:
                extra = f" (same text as {result.duplicate_of})"
            print(f"[{number}/{len(docs)}] {doc.name}: {result.status}{extra}")
    for name, reason in failures:
        print(f"[FAIL] {name}: {reason}")

    try:
        summary = embed_pending(settings.database_url, embedder, progress=print_progress)
    except (EmbeddingAccountError, EmbeddingRateLimited) as exc:
        print(f"STOP: {exc}")
        print("Progress so far is saved. Tell the owner, then run the same command to resume.")
        return 2
    except EmbeddingError as exc:
        print(f"FAILED: {exc}")
        return 1
    print_summary(summary)
    print(
        "documents: "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        + f", failed to load {len(failures)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
