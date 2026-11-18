# RAG Document Chatbot (portfolio project 1 of 6)

A RAG chatbot for a fictional dental clinic ("BrightSmile Dental"). It answers from ingested documents, cites file and page, refuses out-of-scope questions and asks for clarification when a question is vague. FastAPI backend, Postgres + pgvector, Voyage embeddings, Groq LLMs through one OpenAI-compatible client, Streamlit UI. Its `/chat` API is reused by projects 3 and 6, so the RAG logic lives in `app/services/`, independent of routes and UI. Budget is $0: free tiers only, no Anthropic/Claude API. It is shown to clients through a demo video and screenshots, not a live link.

At the start of every session, read @SPEC.md and @WORKFLOW.md and follow them. Log decisions and verified results in @DECISIONS.md. @NOTES.md holds only milestone summaries the owner has approved. Stop at the end of every milestone.

## Machine (measured Nov 16, 2026)

- macOS 15.2, Apple M1 (arm64), 8 cores, 8 GB RAM
- Docker 29.9.2, Compose v5.6.1; Docker VM memory about 4.1 GB
- Python 3.12.16 via `uv` 0.13.6 (pinned in `.python-version`); Node 22.16; git 2.39.5
- Disk: 10 GiB free (re-measured Nov 17, 2026 after the owner removed unused Docker images and containers; it was 3.1 GB free before that and 12 GB on Nov 16). Check `df -h /System/Volumes/Data` before any Docker build; stop below 5 GiB.
- Other local containers use host ports 5432, 5433, 8000, 8080 and 8501. This project uses 5440 (database) and 8010 (API).

## Commands

- Run: `docker compose up -d --build` (API at http://127.0.0.1:8010, health at `/health`)
- Test: `uv run pytest`
- Lint and format: `uv run ruff check .` and `uv run ruff format .`
- Check keys (one tiny call each, prints only OK or FAIL): `uv run python -m scripts.check_keys`
- Ingest a folder: `uv run python -m scripts.ingest_folder data/sample_docs [--dry-run] [--url URL]`; re-embed: `uv run python -m scripts.reembed [--dry-run] [--yes]`; optional local-only seed file (never committed): `uv run python -m scripts.seed build|load|check`
- Threshold calibration (one Voyage request): `uv run python -m scripts.calibrate_threshold`; live smoke test (spends quota): `uv run python -m scripts.smoke_chat [--reasoning-experiment]`
- Regenerate sample PDFs (dev only): `uv run python -m scripts.make_sample_pdfs`
- Database tests use a throwaway `ragdb_test` database on the Compose Postgres (skipped if it is down)
- Demo UI (Streamlit 1.66.0 in the `ui` dependency group; run by hand once by the owner): `uv run --group ui streamlit run ui/streamlit_app.py --server.port 8510` from the project root, no PYTHONPATH needed (port 8510 avoids the ports other containers use); compose service `ui` is behind the profile `ui`; its image builds and reports Streamlit 1.66.0 (owner, Nov 17, 2026, `STREAMLIT_VERSION` now defaults to 1.66.0) but has never been started as a container. Page tests: `uv run --group ui pytest tests/test_ui.py tests/test_ui_import_path.py`.
- Evaluation harness (40 questions approved by the owner Nov 17, 2026; no scores yet): `uv run python -m eval.run_eval --dry-run` (fakes, output is not a result); a live run needs `--confirm-live` (and `--allow-draft` only if any question is DRAFT) and spends Groq/Voyage quota. See eval/README.md. Before a full run, check the clarify step with `uv run python -m scripts.check_clarify --dry-run` (live: `--confirm-live --prompt-version clarify_v2`; not yet run live).

## Rules of thumb

- Never read, print or log `.env` or any key. Do not run state-changing git commands unless asked.
- Tests mock Groq and Voyage and never call them. Live calls are rare smoke tests.
- Prompts live in versioned files under `prompts/`, not inline.
- Embedding dimension and model come from config, never hard-coded.
- Models: the clarify/rewrite step runs on openai/gpt-oss-120b with prompt clarify_v2 (D-078); the answer model is openai/gpt-oss-20b (provisional until the M5 evaluation); the judge is qwen/qwen3.8-27b.
- Free-tier limits: Groq 8K tokens/min per model; Voyage 3 requests/min and 10K tokens/min.
- Design principle (owner, Nov 18, 2026): the free models are weak, so when diagnosing a failure say whether it is a design flaw, a weak-model limit or both, and do not blame the model by default. Prefer code-side guards that fail open over more prompt tuning, and accept free-model limits in a production-style chatbot, documenting them honestly.
