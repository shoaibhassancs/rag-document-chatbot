# DECISIONS

Log of decisions and verified results. Entries are never rewritten: when a decision changes, add a new entry and mark the old one superseded.
Tags: **[Owner]** decided by the owner; **[Assumption]** Claude's proposal the owner accepted; **[Open]** not decided.
"Verified" means read from the provider's own page on Nov 16, 2026 by Claude in this session, or run as a command.

## Decisions

### D-001 Python 3.12 with uv [Owner]
- Decision: Python 3.12 managed by `uv`; the Dockerfile uses the same version.
- Why: spec says 3.11+; 3.11 is not installed; 3.13 risks lagging package support.
- Alternatives: Python 3.13 (installed default), install 3.11.
- Verified: `python3.12` 3.12.16 and `uv` 0.13.6 exist on the machine.

### D-002 Embeddings: Voyage AI `voyage-4`, 1024 dimensions [Owner chose Voyage; Claude proposed the model and size]
- Decision: hosted Voyage embeddings behind a small embedder interface. `EMBEDDING_MODEL=voyage-4`, `EMBEDDING_DIMENSIONS=1024` in config. Column size comes from config/migration. Model name stored with each chunk; startup warns or fails if config and DB disagree; a re-embed command rebuilds vectors from stored text. `input_type=document` for chunks, `query` for questions.
- Why: no PyTorch or model download (lean images, 4 GB Docker VM); free allowance; swappable behind the interface.
- Alternatives: local `sentence-transformers` (heavy download), OpenAI embeddings (second paid-style provider), `voyage-4-lite` (cheaper, lower quality), 2048 dimensions (bigger index for little gain here).
- Verified (Voyage docs): `voyage-4` 32,000-token context; output dimensions 2048, 1024 (default), 512, 256; 200M free tokens per account for `voyage-4`, `voyage-4-lite`, `voyage-4-large`; `input_type` options `None`, `query`, `document` (Voyage prepends a retrieval prompt; embeddings with and without it are compatible); max 1,000 texts and 320K tokens per request for `voyage-4`; all 4-series embeddings are compatible with each other. Our whole knowledge base is far below 200M tokens.
- Not verified: whether signup requires a card (no page I read says so either way); the rate limits of an account with no payment method (the docs table shows "Basic" limits of 2000 RPM / 8M TPM for `voyage-4`, but says Tier 1 requires a payment method, and no free-account figure was found). Ingestion must therefore batch and retry on 429. Free accounts cannot opt out of Voyage storing and training on their data (opt-out needs a payment method); data here is fictional.

### D-003 pgvector index: HNSW, cosine, `vector(1024)` [Assumption]
- Why: pgvector's README limits HNSW and IVFFlat on `vector` to 2,000 dimensions, so 1024 fits without `halfvec`. HNSW needs no training data, unlike IVFFlat, which matters for a small, changing corpus.
- Alternatives: IVFFlat; no index (exact scan, also fine at this size); `halfvec` (only needed above 2,000 dimensions).
- Verified: limits quoted from the pgvector README (HNSW and IVFFlat: `vector` up to 2,000, `halfvec` up to 4,000). Latest tag v0.8.7. Conflicting model-generated summary said "16,000 dimensions"; that is probably the storage limit, not the index limit.

### D-004 One OpenAI-compatible client; separate answer and judge config [Owner]
- Decision: base URL, model and key set separately for the answer model and the judge model. Env names: `MISTRAL_API_KEY`, `GROQ_API_KEY`, `VOYAGE_API_KEY`, `DATABASE_URL`, `APP_API_KEY`; `ANSWER_LLM_API_KEY` and `JUDGE_LLM_API_KEY` reference them.
- Not yet verified: that `${VAR}` expansion works in `.env` for both Docker Compose and the Python settings loader. Check in Milestone 1.

### D-005 Judge and backup: Groq free tier [Owner] (models per role superseded by D-012)
- Decision: Groq, `openai/gpt-oss-120b`, via `https://api.groq.com/openai/v1`.
- Verified (Groq docs): that base URL is documented; limits table shows `openai/gpt-oss-120b` and `openai/gpt-oss-20b` at 30 RPM, 1K requests/day, 8K tokens/minute, 200K tokens/day, which matches the owner's figures. Limits are per organisation and "there may be exceptions". Unsupported OpenAI features: logprobs, logit_bias, N other than 1.
- Not verified: which tab (Free or Developer) that table belongs to (the page text calls them "base limits for the Developer plan"); the models page separately shows 250K TPM for the Developer plan; whether a card is needed; free-plan limits for `llama-3.3-70b-versatile` (listed as Enterprise). Confirm in the Groq console.
- Consequence: 8K tokens/minute and 200K/day force compact prompts and a throttled, resumable eval. A 40-question eval with rewrite, answer and judge calls will probably take more than one day of Groq quota if Groq runs everything. Estimate, not measured.

### D-006 Answer LLM: Mistral free plan [Owner] **SUPERSEDED by D-012 (Groq only)**
- See B-1. The owner's plan was Mistral Small on the free plan with no card.

### D-007 Demo hosting: one Hugging Face Docker Space plus hosted Postgres [Owner] **SUPERSEDED by D-014 (hosting dropped)**
- Owner's plan: one container runs Streamlit and FastAPI; hosted Postgres (Neon or Supabase); `DATABASE_URL` is one setting; local dev uses Compose with a local pgvector Postgres and Streamlit as a Compose service; secrets in Space secrets.
- Verified (HF docs): default exposed port 7860, changeable with `app_port`; multiple external ports need a reverse proxy on a single port, which implies only one public port. So only Streamlit would be public and FastAPI internal; the README would show the API with curl against the local setup. The docs do not state "one public port" as a number.

### D-008 Architecture changes from the owner's diagram review [Owner; Claude agrees]
- `/chat` returns `status`: `answered`, `refused` or `clarify`.
- Content-hash dedup between load and chunk.
- Rewrite the question using session history before retrieval; embed the rewritten question with `input_type=query`.
- Citations built from chunk metadata (file, page), never LLM text.
- URL loading is in the spec already (requirement 1 and in scope), so the diagram keeps "URL".
- Claude's note: the history rewrite is an extra LLM call on follow-up turns. That conflicts a little with the reasons for deferring "clarify before retrieving" (D-011). Proposed to skip it when the session has no history. Not yet measured.
- **Rewrite step superseded by D-013:** the rewrite is now merged with the clarify check into one call that runs on every turn, so the "skip when no history" proposal no longer applies.

### D-009 Other spec changes [Owner]
- "Page" is the PDF page number; null for other formats, with the nearest section heading stored and shown instead.
- `DELETE /documents/{id}` is in scope.
- A prompt-injection test: a document containing "ignore previous instructions" must not change behaviour.
- Prompts live in versioned files under `prompts/`.

### D-010 Accepted assumptions [Assumption, accepted by the owner]
- `pypdf` for PDFs; scanned PDFs out of scope (README note).
- Custom LLM-judge scorer, one or two calls per question, judge from a different family than the answerer; owner hand-checks about 10 verdicts; rubric in `eval/README.md`.
- Session memory in a Postgres table keyed by session ID; last N turns in the prompt; old sessions expire.
- CLI script to ingest a folder; idempotent via the content hash.
- `X-API-Key` required on `/ingest` and `DELETE /documents/{id}`; `/health` stays open; key from `APP_API_KEY`.
- Emails and phone numbers masked in logs.
- Alternatives considered earlier: Ragas (heavy, opaque), PyMuPDF (AGPL), in-process memory (lost on restart).

### D-011 Clarify strategy: deferred [Owner] **SUPERSEDED by D-013 (owner decided to clarify before retrieving)**
- Not accepted yet: "clarify before retrieving". Reasons: one extra LLM call per turn, hurts the 3 s p50 target, spends free quota, handles follow-ups badly.
- Build first: similarity-threshold gate plus one structured LLM call that returns `status`. Then compare both on the vague eval questions and let the numbers decide. Do not build either in Milestones 1 to 3.

## Decisions made on Nov 16, 2026 (second round)

### D-012 LLM provider: Groq only, one model per role [Owner]
- Decision: answer `openai/gpt-oss-120b`; rewrite and clarify `openai/gpt-oss-20b`; judge `qwen/qwen3.8-27b` (a different family). All on Groq's free plan, which the owner's console shows has a separate quota per model: 30 requests/min, 1K requests/day, 8K tokens/min, 200K tokens/day each (owner-reported). One OpenAI-compatible client; provider, base URL, model, key setting, reasoning effort and output cap are separate config values per role (`ANSWER_`, `JUDGE_`, `REWRITE_` prefixes). `MISTRAL_API_KEY` removed from `.env.example`. A `GEMINI_API_KEY` slot exists for optional free-tier overflow and is not built. `allam-2-7b` and `gpt-oss-safeguard-20b` are not used. Llama Prompt Guard might later help as an optional injection check; not built.
- Why: Mistral's free mode has no API access (B-1) and Cerebras is no longer free (owner-reported); Groq is the one provider left that fits $0.
- Alternatives considered: Mistral (needs billing), Cerebras (no longer free), Gemini (kept as an unbuilt overflow slot; limits not verified), both models on one family (judge would grade its own style).
- Verified (Groq docs, Nov 16, 2026): all three model pages exist; limits table lists those models at 30 / 1K / 8K / 200K (the page calls them base limits for the Developer plan, so the owner's console is the source for the Free plan). The `/models` endpoint listed all four configured model ids when I ran the key check (see log).
- Risk: one provider is a single point of failure, and the judge is a preview model (per the models page, Nov 16, 2026).

### D-012a Reasoning effort [Owner asked; Claude verified]
- Verified (Groq reasoning docs): `reasoning_effort` takes `low`, `medium` or `high` for gpt-oss-20b and 120b; for qwen3.8-27b it takes `none`, `default`, `low`, `medium`, `high`. gpt-oss models return reasoning in a `reasoning` field by default; `include_reasoning: false` hides it, and `reasoning_format` is not supported for gpt-oss. The docs say reasoning chains are "part of the token output".
- Decision: default `low` for every role (config values `*_REASONING_EFFORT`), output caps 600 (answer), 400 (judge), 150 (rewrite). Starting values, not tuned.
- Not verified: whether reasoning tokens count against the 8K tokens/min limit. The docs do not say so explicitly. Check response `usage` and rate-limit headers in Milestone 3.
- Not verified: whether `include_reasoning: false` saves tokens (it probably only hides them). Check in Milestone 3.

### D-012b Model fallback [Owner]
- Develop and test on the 20b to save the 120b's daily quota. A config-driven fallback (`ANSWER_FALLBACK_ENABLED`, `ANSWER_FALLBACK_MODEL`) moves the answerer from the 120b to the 20b on the daily cap or repeated 429s, and logs the switch. Built in Milestone 3. Retry with backoff on 429; the user sees "service busy", never a stack trace.

### D-013 Clarify before retrieving, merged with the history rewrite [Owner decided; Claude has concerns]
- Decision: before retrieval, one `gpt-oss-20b` call sees the chat history and the question and returns either a clarifying question or a standalone search query. Then embed, retrieve, apply the threshold; weak matches are refused without an LLM call; otherwise the 120b answers. Milestones 1 to 3 leave a hook for this step; it is built in Milestone 4; its latency, token cost and accuracy on the vague questions are measured in Milestone 5 and recorded here.
- Why: the owner prefers to decide clarification before spending an embedding request and an answer call, and one merged call is cheaper than two.
- Alternatives: threshold gate plus one structured answerer call (D-011's "build first" option); separate rewrite and clarify calls.
- Claude's concerns, to be measured, not assumed:
  1. The call runs on every turn, so it adds latency to every question and puts pressure on the 3-second p50 target.
  2. One call doing two jobs may do each worse; a model biased toward clarifying could ask follow-ups on terse but answerable questions such as "opening hours?".
  3. It needs a strict structured output, and the model must be checked to follow it.
  4. Each turn also spends 8K-tokens/min budget on a history-plus-question prompt, in addition to the answer call.

### D-014 Hosting dropped: not needed [Owner]
- Decision: the project is shown on Fiverr through a demo video and screenshots because Fiverr does not allow links. No live hosting. Live hosting is parked.
- Parked options: Streamlit Community Cloud with a hosted Postgres; Streamlit plus FastAPI on Render free. Hugging Face PRO is excluded by the $0 rule. Hugging Face Docker Spaces need a paid plan to create (verified, B-2) and limit outbound ports (B-3).
- Kept: `DATABASE_URL` is one setting. The RAG logic is in a service layer independent of FastAPI routes and Streamlit, because projects 3 and 6 reuse the `/chat` API.

### D-015 Voyage limits and handling [Owner reported limits; Claude to verify coverage]
- Owner's dashboard (owner-reported): with no payment method, 3 requests/min and 10,000 tokens/min on every model; first 200M tokens free; the banner says "Series 3" while the docs list `voyage-4`. The owner will not add a card.
- Decision: size batches by tokens (about 8K per request at most); rate-limit to 3 requests/min and back off using `Retry-After` on 429; resumable ingestion that skips chunks already embedded (content hash) with progress output; cache query embeddings; the eval runner and re-embed obey the same limits; tests mock Voyage. Built in Milestone 2.
- Contingency only, not built: a local embedder as a fallback.
- Estimate, not measured: ingesting about 30 documents takes roughly 10 to 20 minutes (the 10,000 tokens/min limit is the cause). To be documented in the README.
- Open: after the first real embedding, check the Voyage Usage page to see whether `voyage-4` is covered by the 200M free tokens (a Milestone 1 key check made one real `voyage-4` embedding call of one short word; the owner can look at the Usage page now).

### D-016 Pre-embedded seed file [Claude proposal, to decide in Milestone 2]
- Idea: ship the sample documents' chunks with their vectors so a fresh clone loads in seconds instead of waiting 10 to 20 minutes. Rough size estimate (not measured): about 150 chunks of 1024 float values is roughly 1 to 2 MB as JSON or 0.6 MB in a binary format. That seems sensible. It must record the embedding model and dimension and the chunk content hashes, and be rebuilt whenever the sample documents, chunking or embedding model change. Decide in the Milestone 2 plan.

### D-017 Showcase scope and testing standard [Owner]
- Milestone 7 becomes "Showcase" (UI polish, demo script, screenshot list, README with curl examples, case study, Production notes, clean-clone test). The Testing standard in SPEC.md must pass before Milestone 7. The 3-second p50 target stays until measured in Milestone 5. Expected throughput is about 2 questions/min; one full eval pass needs roughly a day of 120b quota and one to two days of judge quota (owner's estimate).

### D-018 Milestone 1 build choices [Assumption: Claude; routine choices, owner can overrule]
- Python 3.12 pinned in `.python-version`; `requires-python >=3.12,<3.13`; dependencies pinned by `uv.lock`: fastapi 0.163.x, uvicorn 0.54.0, pydantic-settings 2.16.3, psycopg 3.3.9 (binary), httpx 0.28.1; dev: pytest 9.2.0, ruff 0.17.5 (exact versions in `uv.lock`; "fastapi>=0.163.1" is what `uv add` recorded).
- Docker: `python:3.12-slim` base, `uv` 0.13.6 copied from the official image, non-root user, dependencies in their own cached layer. Database image `pgvector/pgvector:0.8.7-pg17` (the tag exists on Docker Hub; pgvector 0.8.7 matches the latest tag I read). The `vector` extension is created by `db/init/001-extensions.sql` on first start. Tables come in Milestone 2.
- Host ports: other local containers already use 5432, 5433, 8000, 8080 and 8501, so this project maps the database to 5440 and the API to 8010 (`DB_HOST_PORT`, `API_HOST_PORT`). Inside Compose the API reaches the database at host `db` through an overridden `DATABASE_URL`. I did not touch the other containers.
- Role keys are resolved by name (`*_LLM_API_KEY_VAR=GROQ_API_KEY`) instead of `${VAR}` expansion in `.env`, which closes the D-004 doubt about expansion in Compose and the Python loader. The API key values are `SecretStr`, so printing settings does not show them.
- `/health` returns 200 `ok`, 200 `degraded` (a key is missing) or 503 `down` (database or pgvector missing). It checks that keys are set and makes no paid calls. Errors are reported by exception class name only, so a connection string can never leak.
- `scripts/check_keys.py`: Groq via `GET /models` (uses no tokens) plus a check that all configured models are listed; Voyage via one embedding request that also checks the vector length. Prints only OK or FAIL with a non-secret reason.
- No hook code for the clarify step was added in Milestone 1: the settings carry a `rewrite` LLM role and the spec fixes `app/services/` as its home, and a placeholder file would break the "no placeholder files" rule. The first real hook appears with the service layer in Milestone 3.

## Decisions made after Milestone 1 review [Owner]

### D-019 Milestone 1 approved; git stays with the owner
- `.vscode/` and `*.code-workspace` added to `.gitignore`. Claude runs no git commands; the owner runs `git init` and the first commit.

### D-020 Voyage `voyage-4` is covered by the free allowance [Owner-reported]
- Owner's Usage page: `voyage-4` shows 0 used and 200,000,000 free tokens remaining. Do not switch the embedding model. Any billing or quota error from Voyage is a stop-and-tell-the-owner event. After the first real ingestion the owner checks the Usage page and reports the count.

### D-021 Merged rewrite and clarify call: leans toward answering [Owner]
- Agreed to merge (D-013). The prompt is worded to lean toward answering, and the eval set includes terse but answerable questions (for example "opening hours?"). The 3-second p50 target stays and is measured in Milestone 5.

### D-022 Streamlit host port [Owner]
- Port 8501 is used by another container, so Milestone 6 gives Streamlit a configurable host port, default 8511.

## Blockers found during verification (all resolved by the second round)

### B-1 Mistral free plan has no API access [RESOLVED: owner dropped Mistral, see D-012]
- Found: Mistral's "Usage and limits" page says "Free mode ... doesn't include API access. Creating API keys and using the API require billing activation and pay-as-you-go", drawing on prepaid credits. Source: docs.mistral.ai/admin/billing-usage/usage-limits. This is one page and may be incomplete; the older help-center page and the aggregator sites that described a free API tier were 404 or third-party. Phone verification and the data-training statement could not be confirmed.
- Also found: the OpenAI-compatible endpoint is `https://api.mistral.ai/openai/v1` and is "in public preview"; the native endpoint is `https://api.mistral.ai/v1`. `.env.example` uses the OpenAI-compatible URL; the model name is left blank until B-1 is settled.
- Options (none chosen):
  1. Check your own Mistral console: if it lets you create an API key with no card, the docs page is out of date and nothing changes. Cheapest test.
  2. Answerer and judge both on Groq with different families, for example `openai/gpt-oss-120b` for answers and the preview `qwen/qwen3.8-27b` for judging. Stays at $0, one provider to manage, but both share one organisation's limits and the judge is a preview model.
  3. Another free-tier provider (for example Google Gemini or OpenRouter free models). I have not verified their current free limits or terms.
- Claude's recommendation: try option 1 first (a few minutes), otherwise option 2.

### B-2 Creating a Hugging Face Docker Space needs a paid plan [RESOLVED: hosting dropped, see D-014]
- Found (hf docs, spaces-overview): "Gradio and Docker Spaces run on compute and require a paid plan to create: PRO for personal accounts, Team or Enterprise for organizations." Static Spaces are free. CPU Basic hardware (2 vCPU, 16 GB, 50 GB non-persistent disk) has no hourly cost, but a Space on it sleeps after about 48 hours of inactivity.
- Not verified: whether the Streamlit SDK type (not named in that sentence) can be created on a free account.
- Options: check if a Streamlit-SDK Space is free; use another free host (not researched: Streamlit Community Cloud, Render, Fly.io, others); or ship the demo as a video plus a `docker compose up` README and treat a live link as optional, which would need an edit to the definition of done.

### B-3 Hugging Face Spaces limit outbound ports [RESOLVED: hosting dropped, see D-014]
- Found (reported by the fetch agent quoting the Spaces overview; not re-read by me): outbound requests are allowed only on ports 80, 443 and 8080. Neon and Supabase Postgres normally use port 5432 (and Supabase's pooler another port), so a Space could not reach them. I have not checked those providers' ports or alternatives.
- Possible route: run Postgres with pgvector inside the same container and rebuild the index from `data/sample_docs` on start (the disk is not persistent anyway). That fits $0 but is a design change to decide at Milestone 7.

## Verified results log

| Date | Check | Result |
| --- | --- | --- |
| Nov 16, 2026 | Machine: `sw_vers`, `sysctl`, `df`, tool versions | macOS 15.2, M1, 8 GB, Docker 29.9.2, Compose v5.6.1, Python 3.12.16 and 3.13.5, uv 0.13.6; disk 7.4 GB free |
| Nov 16, 2026 | `df -h` after the owner freed space | 14 GB free |
| Nov 16, 2026 | Voyage docs (pricing, embeddings, rate-limits, FAQ, API key pages) fetched with curl; the fetch tool failed on them | see D-002 |
| Nov 16, 2026 | Groq docs (rate-limits, models, openai compatibility) via fetch tool | see D-005 |
| Nov 16, 2026 | Mistral docs (usage-limits, pricing, chat-completion) via curl | see B-1 |
| Nov 16, 2026 | pgvector README via fetch tool; HF Spaces docs via fetch tool and curl | see D-003, B-2, B-3 |
| Nov 16, 2026 | `cmp` of WORKFLOW.md against `ai_engineer_build_prompt.md` | original text is an exact prefix; addendum appended after |
| Nov 16, 2026 | `.gitignore` | written but not tested, because the folder is not a git repository and I do not run `git init` |
| Nov 16, 2026 | Folder move: grep for "Desktop" or "/AIAgents" in project files; `cmp` of the original prompt against `WORKFLOW.md` | no old paths found; original prompt is an exact prefix of `WORKFLOW.md` |
| Nov 16, 2026 | Groq reasoning, rate-limit and model docs via curl | see D-012, D-012a |
| Nov 16, 2026 | `uv run ruff check .` / `uv run ruff format --check .` | all checks passed |
| Nov 16, 2026 | `uv run pytest -q` | 22 passed (config 8, health 5, check_keys 9), 1 warning: starlette says `httpx` in its TestClient is deprecated in favour of `httpx2`; not acted on |
| Nov 16, 2026 | `docker compose up -d --build`; `curl http://127.0.0.1:8010/health` | HTTP 200 `{"status":"ok","database":{"ok":true,"pgvector":"0.8.7"},"keys_configured":{"groq":true,"voyage":true}}` |
| Nov 16, 2026 | `docker compose stop db`, then `/health`, then start | HTTP 503 `down` with `OperationalError`; after restart HTTP 200 |
| Nov 16, 2026 | Live `uv run python -m scripts.check_keys` (once) | `groq: OK`, `voyage: OK` (one `voyage-4` embedding request of one word; Groq `/models` call) |
| Nov 16, 2026 | Image sizes | API image 406 MB; pgvector image 676 MB |
| Nov 16, 2026 | First `docker compose up` | failed: host port 5432 in use by an existing Postgres; then 5433 and 8000 in use by other projects' containers. Fixed by moving to 5440 and 8010 (D-018) |
| Nov 16, 2026 | A multi-file shell command was refused by a built-in safety check (it contained no removal) | nothing ran; I created the files with the Write tool instead |

## Open decisions

- None blocking. To settle: whether `voyage-4` is covered by the free 200M tokens (owner checks the Usage page, D-015); the pre-embedded seed file (D-016, Milestone 2); whether reasoning tokens count against Groq's per-minute limit (D-012a, Milestone 3); whether clarify-before-retrieving meets the latency and accuracy goals (D-013, Milestone 5).
- Closed: provider (D-012), clarify strategy (D-013), hosting (D-014: "not needed: showcase is video and screenshots; live hosting parked").
