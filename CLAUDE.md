# RAG Document Chatbot (portfolio project 1 of 6)

A RAG chatbot for a fictional dental clinic ("BrightSmile Dental"). It answers from ingested documents, cites file and page, refuses out-of-scope questions and asks for clarification when a question is vague. FastAPI backend, Postgres + pgvector, Voyage embeddings, Groq LLMs through one OpenAI-compatible client, Streamlit UI. Its `/chat` API is reused by projects 3 and 6, so the RAG logic lives in `app/services/`, independent of routes and UI. Budget is $0: free tiers only, no Anthropic/Claude API. It is shown to clients through a demo video and screenshots, not a live link.

At the start of every session, read @SPEC.md and @WORKFLOW.md and follow them. Log decisions and verified results in @DECISIONS.md. @NOTES.md holds only milestone summaries the owner has approved. Stop at the end of every milestone.

## Machine (measured Nov 16, 2026)

- macOS 15.2, Apple M1 (arm64), 8 cores, 8 GB RAM
- Docker 29.9.2, Compose v5.6.1; Docker VM memory about 4.1 GB
- Python 3.12.16 via `uv` 0.13.6 (pinned in `.python-version`); Node 22.16; git 2.39.5
- Disk: 12 GB free (re-measured Nov 16, 2026 after the folder move and Docker image pulls)
- Other local containers use host ports 5432, 5433, 8000, 8080 and 8501. This project uses 5440 (database) and 8010 (API).

## Commands

- Run: `docker compose up -d --build` (API at http://127.0.0.1:8010, health at `/health`)
- Test: `uv run pytest`
- Lint and format: `uv run ruff check .` and `uv run ruff format .`
- Check keys (one tiny call each, prints only OK or FAIL): `uv run python -m scripts.check_keys`
- Planned, not built yet: `scripts.ingest_folder`, `scripts.reembed`, `eval/run_eval.py` (throttled and resumable)

## Rules of thumb

- Never read, print or log `.env` or any key. Do not run state-changing git commands unless asked.
- Tests mock Groq and Voyage and never call them. Live calls are rare smoke tests.
- Prompts live in versioned files under `prompts/`, not inline.
- Embedding dimension and model come from config, never hard-coded.
- Free-tier limits: Groq 8K tokens/min per model; Voyage 3 requests/min and 10K tokens/min.
