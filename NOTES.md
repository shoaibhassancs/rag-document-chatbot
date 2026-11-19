# NOTES

Milestone summaries, written in plain English. Each is added only after the owner has read and approved it.

## Milestone 1: Setup

### What this milestone does
Milestone 1 builds the foundation and nothing else. It starts a small web service (the API) and a database that can store vectors. It has one working endpoint, `/health`, which reports whether the system is ready. It also has a script that checks the two API keys work. There is no chatbot yet, no documents and no AI behaviour. The aim was to prove the base stack runs, so every later milestone builds on something tested.

### How it works
- **Two containers, started with one command.** `docker compose up -d --build` starts the API (FastAPI) and Postgres with the pgvector extension, which adds a "vector" column type. pgvector is what lets Postgres later store and search embeddings.
- **Settings come from `.env`.** One settings file reads `.env` and the environment. API keys are held as secret values, so printing the settings never shows them.
- **Each AI job has its own setting group.** The answerer (`gpt-oss-120b`), the rewrite/clarify step (`gpt-oss-20b`) and the judge (`qwen3.8-27b`) each have their own provider, URL, model, key, reasoning effort and output cap. Switching a role to another provider is a config change, not a code change.
- **`/health` reports readiness without spending quota.** `ok` when the database, pgvector and both keys are in place; `degraded` (HTTP 200) when a key is missing but the database works; `down` (HTTP 503) when the database or pgvector is missing. It only checks that keys are *set*. It never calls Groq or Voyage, so it can't use up free-tier limits.
- **`scripts/check_keys.py`** makes one tiny call per provider and prints only OK or FAIL. For Groq it lists models (uses no tokens) and confirms the four configured models exist. For Voyage it embeds one word and checks the vector has 1024 numbers.

### Key decisions and why
| Decision | Why |
| --- | --- |
| Python 3.12 via `uv`, pinned | Matches the spec; 3.13 can lag on package support |
| Groq only, one model per role | Mistral's free plan has no API access and Cerebras isn't free, which leaves Groq for a $0 budget |
| Judge from a different model family | So the judge doesn't grade its own style |
| Keys resolved by name (`GROQ_API_KEY`), not `${VAR}` expansion | Avoids relying on expansion behaving the same in Compose and Python |
| Host ports 5440 (database) and 8010 (API) | Other containers already use 5432, 5433 and 8000 |
| Reasoning effort `low`, with output caps | Groq's reasoning models "think" before answering and those tokens are output; the limit is 8K tokens/min |
| No clarify-hook code yet | A placeholder file would break the "no placeholders" rule; the real hook arrives with the service layer in Milestone 3 |

### AI and engineering concepts involved
- **Embedding dimension.** An embedding is a list of numbers that represents a text's meaning. Ours has 1024, set in config and never hard-coded.
- **Rate limits and tokens.** Free tiers cap requests and tokens per minute (Groq: 8K tokens/min per model; Voyage: 3 requests/min and 10K tokens/min). That shapes batching and prompt design from now on.
- **Reasoning tokens.** gpt-oss models think before answering. Groq's docs say this reasoning is part of the output, but not whether it counts against the per-minute limit. Milestone 3 will test that.
- **OpenAI-compatible client.** Groq speaks the same protocol as OpenAI, so one client works for every role.
- **Mocking.** Tests replace Groq and Voyage with fakes, so they run fast, free, and never touch the network.

### What was verified
- `ruff` clean and 22 tests pass (config 8, health 5, key check 9).
- `docker compose up -d --build` starts both containers.
- `/health` returns 200 `ok` with pgvector 0.8.7 and both keys configured.
- With the database stopped, `/health` returns 503 `down`; after a restart it returns 200 again.
- The real `check_keys` run printed `groq: OK` and `voyage: OK`.
- No key-shaped strings exist anywhere outside `.env`.
- Owner-reported: the Voyage Usage page shows `voyage-4` with 200M free tokens remaining.

### What to watch out for
- `.gitignore` is untested because the folder was not a git repo at the time.
- Whether reasoning tokens count against Groq's per-minute limit is unchecked (Milestone 3). The Groq limits come from the owner's console, which is the authority; Groq's public table labels them "Developer plan".
- `qwen3.8-27b` is a preview model and could change or disappear. With Groq as the only provider, an outage or quota hit stops everything.
- If another container uses host port 5440 or 8010, the stack won't start. Both ports are configurable.
- Tests don't touch the real database yet; the health tests use a fake database checker, and the real database was checked by hand with curl.
- Starlette warns that `httpx` in its TestClient is deprecated in favour of `httpx2`. Not acted on.
- `.gitignore` lists `NOTES.md` under "Owner notes (kept local)", so this file will not be committed.

## Milestone 2: Ingestion

### What this milestone does
Milestone 2 turns documents into searchable knowledge. It reads PDF, Markdown, text and web pages, cleans them, cuts them into chunks, turns each chunk into an embedding (a list of 1024 numbers that stands for its meaning) and stores everything in Postgres. It adds the endpoints to upload, list and delete documents, and two command-line tools: one to ingest a folder and one to rebuild all embeddings. There is still no chat and no answering. The demo clinic's 30 sample files are ingested and ready for search.

### How it works
- **Loading.** Our own small loaders read each format. A PDF page keeps its page number, with running headers and footers removed. Markdown and web pages keep the nearest heading as the section. A web page is fetched from one URL, only if it is http or https, small, and not on a private or local address (unless `ALLOW_PRIVATE_URLS=true`).
- **Duplicates.** Each document gets a hash of its text with all spacing collapsed. Same name and hash: skipped as unchanged. Same name, new text: updated, and unchanged chunks keep their vectors. Different name, same text: skipped as a duplicate. This is how `faq-printed-copy.md` is caught.
- **Chunking.** Pages never mix. Markdown splits at `#` and `##` headings, and very small sections are merged. Chunks are up to about 600 tokens with overlap. Sizes are estimates (characters divided by 4).
- **Embedding under the free limits.** Voyage allows 3 requests and 10,000 tokens per minute. A limiter enforces both. Chunks are batched by token count, about 8K per request. After each batch the vectors are saved, so a crash loses nothing and a rerun continues. Identical chunk text is embedded once. A rate-limit error (429) waits and retries. A 401, 402 or 403 stops everything with a message to tell the owner.
- **Storage.** Chunks are saved first without vectors, then embedded. The vector size comes from config and appears in one place, the migration. Search uses an HNSW index with cosine distance.
- **Startup safety.** The API runs the migration, then refuses to start if the configured embedding model or size differs from what the database was built with, or if a re-embed was left unfinished. This stops a silent mix of vectors from different models.
- **API.** `POST /ingest` (key required) checks the files straight away, rejects bad ones with a reason, answers 202 and embeds in a background worker. `GET /documents` shows status and progress. `DELETE /documents/{id}` (key required) removes a document and its chunks, and refuses with 409 while it is still being embedded.
- **Tools.** `scripts.ingest_folder` (with `--dry-run` to estimate cost and time first), `scripts.reembed` (rebuilds every vector from stored text; changing the vector size needs `--yes`) and `scripts.seed` (optional, local only).

### Key decisions and why
| Decision | Why |
| --- | --- |
| Own loaders plus `langchain-text-splitters` only | Lighter and clearer than `langchain-community`; the logic stays in our code |
| Plain SQL migrations | Alembic would add SQLAlchemy for a four-table schema |
| 202 plus a background worker | A throttled ingest takes minutes, too long for one web request |
| Fail at startup on an embedding mismatch | Mixed vectors give wrong search results without any error |
| Heading-aware chunks, mean about 250 tokens | Keeps the section heading exact; compared with 600-token packing in Milestone 5 |
| Seed file never committed | A full ingestion takes only about 2 minutes, and Voyage's terms on returned embeddings were not found |
| Hash of whitespace-collapsed text | A re-saved copy with different spacing is still recognised |

### AI and engineering concepts involved
- **Chunking.** Documents are cut into pieces because search and prompts work on small passages. Too small loses context, too big dilutes meaning.
- **Embedding and `input_type`.** Documents are embedded as `document` and questions will be embedded as `query`, so they land close together in the vector space.
- **Cosine distance and HNSW.** Cosine measures the angle between two embeddings. HNSW is an index that finds the nearest ones quickly.
- **Throttling and idempotency.** Rate limits shape the design: token-sized batches, saved progress, safe reruns. Idempotent means running it twice gives the same result.
- **Prompt injection stays text.** The fixture document containing "ignore previous instructions" is stored as ordinary text. Whether the bot obeys it is tested in later milestones.

### What was verified
- `ruff` clean and 189 tests pass. Tests cover loaders, chunker, limiter, the Voyage client against mocked replies, the database (a throwaway `ragdb_test`), the API, re-embedding, the command-line tools and the seed file. A test confirms outbound network calls are blocked.
- The real stack applied the migration, and the API refuses to start with a different model or vector size.
- One real ingestion: 89 chunks in 4 requests, 21,363 actual tokens against 23,617 estimated, 2 minutes 4 seconds. All 29 documents are `ready`. A repeat run made 0 requests.
- Two real queries returned the right documents first (Delta Dental to the insurance PDF, extraction diet to the aftercare PDF). This was a sanity check, not retrieval.
- Voyage returned real 429s with no `Retry-After` header, and the fallback wait recovered.
- Owner will check the Voyage Usage page and report the count.

### What to watch out for
- Chunks average about 250 tokens, below the original 500 to 800 target (spec updated, D-026). Retrieval quality comparison is in Milestone 5.
- The meaning of Voyage's 401, 402 and 403 is unverified, so all three stop the run.
- The limiter lives inside one process. Running the CLI while the API is embedding can cause extra 429s that backoff absorbs.
- `DELETE` was tested against a real Postgres but not by hand on the demo data, to avoid repeating Voyage spend.
- The API image is 523 MB, up from 406 MB, because of `langchain-text-splitters` and its dependencies.
- A new install runs `scripts.ingest_folder` (about 2 minutes).

## Milestone 3: Retrieval and answers

Approved by the owner, Nov 17, 2026.

### What this milestone does
Milestone 3 makes the chatbot answer. A question goes to `POST /chat`. The system turns the question into an embedding, finds the five most similar chunks in the database, drops weak matches, and asks a language model to answer using only those chunks. The reply has a status (`answered`, `refused` or `error`) and a list of sources. If nothing in the documents is close enough, it refuses without calling the model. Clarifying questions and conversation memory arrive in Milestone 4.

### How it works
- **Retrieval.** The query is embedded (cached for repeats), then pgvector returns the top five chunks by cosine similarity. A similarity threshold of 0.40 drops weak chunks. If none remain, the reply is a fixed refusal and the model is never called.
- **Answering.** The remaining chunks are numbered and sent with the question under a versioned prompt (`prompts/answer_v2.txt`). The model must reply in JSON: whether the excerpts answer the question, the answer, and which excerpts it used. If it says they don't, the reply is a refusal.
- **Citations are built in code** from the file, page and section of the chunks the model used, never from text the model writes. If the model names an invalid excerpt, only the top-scoring chunk is cited and a warning is logged.
- **Text is data.** Chunk text and the question are wrapped in tags and never treated as instructions. Text that imitates our tags is neutralised.
- **One LLM client for every role.** It sends requests to Groq, waits and retries on short rate limits, switches to a fallback model on a daily cap or repeated 429s, retries server errors twice, retries once when Groq reports it could not generate valid JSON, and reports token use. Errors never show a stack trace; the user sees a configurable "busy" message with HTTP 503.
- **Voyage wait cap.** A chat query waits at most about 20 seconds for the embedding rate limit before returning the busy reply. Ingestion keeps its long schedule.
- **Service layer.** The logic lives in `app/services/chat.py` with no FastAPI imports. A hook is ready where Milestone 4 plugs in the clarify call.

### Key decisions and why
| Decision | Why |
| --- | --- |
| Similarity threshold 0.40, provisional | Just below the lowest score a needed chunk had on the 16 answerable calibration questions. It was tuned on the same 28 questions, so Milestone 5 re-checks it on a separate set |
| The prompt is the real guard against off-topic answers | No threshold separates dental-adjacent out-of-scope questions (antibiotics, Invisalign) from real ones, because their scores overlap |
| Prompt v2 adds one generic rule: answer only exactly what was asked | On the 120b it fixed the home-address question but not the antibiotics one; the 20b refused both |
| Default answer model `gpt-oss-20b`, provisional | It refused every out-of-scope question in the comparison. The 120b stays a candidate and the fallback. Both have the same free limits, so this is not about quota. The final choice is made in Milestone 5 from numbers |
| One retry on Groq's "Failed to generate JSON" error (provisional) | The 20b hit it on 2 of 6 calls on one question; without a retry a visitor would see the busy message |
| `error` status with HTTP 503 | A model failure should look different from "not in the documents" |
| Refusal and busy texts are configurable | The wording is a client-facing choice |

### AI and engineering concepts involved
- **Retrieval-augmented generation (RAG):** fetch relevant text first, then let the model answer from it.
- **Similarity score and threshold:** how close two embeddings are, and the cut-off for "too weak to use".
- **Grounded prompting and structured output:** the model is told to use only the excerpts and to return JSON that code can check.
- **Prompt injection:** text in documents or questions that tries to give orders. It is treated as data.
- **Temperature 0 and repeatability:** temperature 0 does not make outputs identical. The wording of the same answer varied between identical runs.
- **Fallback and backoff:** how the client survives free-tier limits.

### What was verified
- 319 tests pass and ruff is clean. Tests never call Groq or Voyage.
- Real calls: the threshold was calibrated on 28 questions with one Voyage request. Live smoke runs on both models. Under prompt v1 the 120b answered the antibiotics and home-address questions wrongly.
- Prompt comparison on the 28 questions, three combinations. In all three, all 16 answerable questions were answered with an expected source cited. Wrong answers to out-of-scope questions: v1/120b two, v2/120b one, v2/20b none.
- Stability check (prompt v2, three runs per question). The 120b answered the antibiotics question wrongly 3 of 3 times. The 20b refused every out-of-scope question in every call that completed; 2 of its 6 calls on the injection question returned Groq's 'Failed to generate JSON' error, and its set is incomplete (no clean 3 x 6 run).
- Live latency on the 20b (median, 9 questions): embedding 1.2 s, search 0.1 s, model 0.65 s, total about 1.8 s.
- Owner-reported: Voyage usage after ingestion was 21,377 tokens, matching the estimate of about 21,400.

### What to watch out for
- The threshold and the prompt were tuned on the same 28 questions. Milestone 5 uses a separate set.
- The 20b returned Groq HTTP 400 "Failed to generate JSON" on 2 of 6 calls on the injection question. A one-retry fix is built and tested with mocks but not yet verified live.
- The 20b stability set is incomplete: the runs were stopped twice by that error.
- Model outputs are not exactly repeatable, so one run is weak evidence.
- The answer-model choice between the 20b and 120b is provisional.
- Over-citation: one clear case (the 20b citing an extra file on the numbness question). The other extra citations looked like gaps in the expected-source lists.
- Answer correctness in the comparison was spot-checked by Claude, not fully verified.
- Whether Groq counts reasoning tokens against the per-minute limit is still inconclusive (about 300 tokens of uncertainty). The plan is to budget prompt plus the 600-token cap per call.

## Milestone 4: Guardrails and memory

Approved by the owner, Nov 17, 2026.

### What this milestone does
Milestone 4 makes the chatbot behave in conversation. It remembers the last few exchanges of each session, so a follow-up like "and on Saturdays?" is understood. Before searching, one small model call decides whether the question is clear enough to search or needs one short clarifying question ("How much is it?" gets "Which treatment do you mean?"). Every exchange is logged to the database for later evaluation, with emails and phone numbers masked. The `/chat` reply gains a working `clarify` status; its shape did not change.

### How it works
- **Memory.** Postgres keeps the last 4 exchanges per session id. A session idle for more than 24 hours is treated as new straight away, and expired sessions are deleted lazily (on the first chat, then at most every 10 minutes). There is no background job.
- **One call decides: search or clarify.** `gpt-oss-20b` (the rewrite role) sees the recent history and the new message and returns JSON: an action (`search` or `clarify`), a standalone search query, and a clarifying question if needed. The prompt is a versioned file, `prompts/clarify_v1.txt`.
- **The model proposes, the code decides.** A clarifying question is accepted only if it is one short question. The system never asks two clarifying questions in a row (it remembers the status of the previous reply). If the call fails or returns something unusable, the system searches with the message as written, so the clarify step can never block an answer. When in doubt, it answers.
- **The standalone query is what gets searched and answered.** The user's own words are what is stored in memory and in the log.
- **A `clarify` reply does nothing else.** No embedding, no search, no answer call, no sources.
- **Masking.** Emails become `[EMAIL]` and phone numbers `[PHONE]` in the stored memory, in the log table and in application log output. Prices, dates, years, page numbers and ZIP codes are left alone (tested). The reply to the user is never masked.
- **Logging.** One row per exchange in `chat_logs`: question, standalone query, retrieved and used chunk ids, similarity scores, status, answer, timings, token use, and the prompt versions. No keys, no prompt text. A logging failure never changes the reply.
- **Text is data, again.** The user's message and the history are wrapped in tags and tag-like text is escaped, so they cannot break out of the data section. Whatever the model returns as a query is only ever a search string.
- **Switches.** `CLARIFY_ENABLED=false` skips the extra call. History length, session lifetime and prompt version are settings.

### Key decisions and why
| Decision | Why |
| --- | --- |
| One merged clarify-and-rewrite call | The spec's design: one small call before retrieval, so follow-ups and vague questions are handled together |
| Code enforces the clarify rules, not only the prompt | A model can ignore a prompt; "never two in a row" and "when in doubt, answer" must hold every time |
| Fail open | A broken clarify step should degrade to plain search, not to errors |
| Stored memory is masked (provisional) | Personal data is not kept at rest. Cost: a follow-up cannot refer back to an exact email or phone number |
| The answer step gets the standalone query (provisional) | So a follow-up is answered as a full question |
| No shortcut that skips the call for clear questions (provisional) | The spec measures the call's cost and accuracy in Milestone 5 |
| No fallback model for the rewrite role (provisional) | A failure there falls back to plain search instead |
| Lazy expiry of sessions (provisional) | No background thread or new package needed |
| Rewrite output cap raised from 150 to 250 tokens | Room for a query plus a question plus short reasoning |

### AI and engineering concepts involved
- **Query rewriting.** A follow-up has no meaning alone; the model turns it into a standalone question using the history.
- **Clarify before retrieving.** Asking costs one reply from the user, searching on a vague question wastes an answer; the system asks only when it must.
- **Guardrails in code versus in the prompt.** The prompt asks the model to behave; the code verifies and overrides.
- **Fail-open design.** If a helper step breaks, the main path still works.
- **Short-term memory and expiry.** A small window of recent turns, forgotten after a day.
- **PII masking.** Removing emails and phone numbers before they are stored, with tests for what must not be masked.
- **Prompt injection through the user and the history.** Treated as data and validated by code.

### What was verified
- `ruff` clean and 410 tests pass (319 before). Groq and Voyage are mocked; the database tests use a throwaway Postgres database, never the real one.
- Tests cover: masking formats and false positives; memory order, window, isolation, expiry and purge; every malformed clarify reply becoming a search; never two clarifies in a row; fail-open on bad JSON, cut-off replies, 503 and 401; a `clarify` reply making no embedding, search or answer call and having no sources, also over HTTP; one log row per exchange; memory and log failures not changing the reply; a missing session id (no memory, still logged); a document and a user message that give orders staying inside the data tags, with a simulated obedient model still getting citations only from chunk metadata.
- Not applied to the real database: the migration `002_memory_and_logs.sql` is applied automatically at the next API restart.

### What to watch out for
- Nothing ran against the real Groq. How the real 20b behaves with the clarify prompt (asking too often, writing poor queries, the Groq "Failed to generate JSON" error on this call) is unknown until a live check.
- Every question now costs two Groq calls on the same 20b model, which shares one 8K tokens per minute quota. Estimated throughput is about 2 questions a minute at best (about 1.8 in the worst case). Whether the rewrite role should use a different model with its own quota is an open decision for Milestone 5.
- The extra call adds latency before retrieval (estimated 0.6 to 1 s). The spec's p50 target of 3 seconds must be re-measured live.
- If the clarify step fails, the system quietly searches without it; `/health` does not report that.
- A bare range like "450-1290" without a dollar sign can be masked as a phone number. The clinic's own phone number in an answer is masked in the logs, never in the reply.
- The answer step now sees the rewritten query. If the rewrite is poor, retrieval could do worse than on the raw message; Milestone 5 can compare.

## Milestone 5: Evaluation

### What this milestone does
Milestone 5 measures the chatbot. It adds 40 owner-approved questions with expected answers (29 answerable, 7 out of scope, 4 vague), a throttled, resumable evaluation runner, an LLM judge, a clarify-step check and a threshold calibration tool. It produced a baseline (Run A), one general fix pass, and two re-runs: runA2 (answer model gpt-oss-20b) and runB (gpt-oss-120b). The answer model was chosen from them: gpt-oss-20b is final.

### How it works
- **Runner.** `eval/run_eval.py` asks the real chat service each question, saves progress after every unit, and stops rather than mix two models. It records the answer model, prompt versions, gate and question-file hash in every result file.
- **Scoring.** Code scores retrieval hit rate (all or any expected source in the top 5), refusal accuracy, status accuracy and latency. The judge (`qwen/qwen3.8-27b`, a different family from the answerer) scores correctness against the expected answer and faithfulness against the retrieved chunks, 0 or 1 each. A failed judge call is an error, never a score.
- **Fix pass (D-081 to D-083).** After Run A: the answer model receives the user's original words when there is no history; the original words are searched when there is no history; a guard turns a clarifying question that only asks about words already in the message into a search; the gate went from 0.40 to 0.33 (chosen by hand, thin margin). Each was checked on practice data first.
- **Judge reliability (D-085).** JSON failures are read from the error body or retried without JSON mode; 429s honour Retry-After; error reasons are precise. Prompts and the 0/1 rule did not change.

### Results (judge scores; one run each; 40 questions)
| Metric (target) | Run A | runA2 (20b) | runB (120b) |
| --- | --- | --- | --- |
| Retrieval hit, all sources (90%) | 69.0% | 79.3% | 82.8% |
| Faithfulness (90%) | 100% | 100% | 100% |
| Correctness, judge (85%) | 39.3% | 69.0% | 71.4% |
| Correctness, owner-reviewed (85%) | not recomputed | 72.4% | 75.9% |
| Refusal accuracy (95%) | 69.4% | 80.6% | 83.3% |
| Status accuracy | 55.0% | 72.5% | 75.0% |
| Latency p50 / p95 | 1.6 / 2.6 s | 2.1 / 2.9 s | 2.2 / 3.5 s |

Met: faithfulness, latency. Not met: retrieval hit rate, correctness, refusal accuracy. Vague handling went from 100% to 50% on purpose (the guard).

### Key decisions and why
| Decision | Why |
| --- | --- |
| Final answer model gpt-oss-20b (D-087), 120b is fallback | Equal within noise, lower p95 latency, quota split across two models |
| One fix pass only, then stop | More tuning on the same 40 questions would make the numbers meaningless |
| Gate 0.33 | Ceiling of truly unrelated and vague practice questions (0.300) plus 0.03; margin is thin and the case for lowering it comes partly from Run A |
| Keep the clarify guard although it costs vague questions | It rescues clear questions Run A wrongly clarified; the owner accepted the cost knowingly |
| Judge prompts not changed after seeing results | Changing the judge after seeing answers would be tuning |

### AI and engineering concepts involved
Retrieval hit rate, faithfulness versus correctness, LLM-as-judge and its failure modes, negative controls for a judge, similarity-threshold gating, non-determinism and run-to-run noise, held-out versus tuned-on data, rate-limit handling.

### What was verified
- The owner ran Run A, runA2 and runB; the numbers above are owner-reported.
- The owner hand-checked the judge: 10 of Run A's judged verdicts on Nov 18 (9 agreed), and on Nov 19 every judge 0, the unjudged items and three passes. One judge 0 (E31) was overturned in both runs, others upheld. Corrections can only raise scores.
- Judge controls (`scripts/check_judge.py`, 12 hand-written controls from the sample documents): owner-run on Nov 18 and again on Nov 19 after the D-085 change; both times 9 of 9 wrong or unsupported controls were scored 0 and 7 of 7 good controls were kept, with no miss. This shows the judge is not a rubber stamp; the controls are clear-cut, so it does not prove the judge catches subtle errors.
- 787 tests pass with Groq and Voyage mocked.

### What to watch out for
- **Not a held-out estimate.** The fixes were chosen after seeing Run A on the same 40 questions.
- Single runs have noise of about one or two questions; the two models cannot be separated.
- Failures by cause. Design: E04 and E17 retrieval misses; E37 and E39 searched instead of clarified. Clarify model plus design: E08 and E26. Weak-model limit: E21 and E27 (20b) and E16 (120b) refused with the answer in context. E30 got a clarify instead of a refusal.
- The judge is an LLM; it is strict about omitted side details and two calls kept failing with `reply_cut_off`.
- Two judge verdicts were filled in by hand (runA2 E20 faithfulness, runB E21 correctness).

## Milestone 6: UI and packaging

### What this milestone does
Milestone 6 adds a Streamlit chat page, the fuller test suite and the README, and checks the packaging. The UI is a client of the `/chat` and document APIs; it holds no RAG logic.

### How it works
- **Page.** `ui/streamlit_app.py` shows the three outcomes (answered, refused, clarify) and error with different badges, sources under each answer and a documents panel. API calls live in `ui/api_client.py`, display logic in `ui/view.py`; model and document text is drawn as plain text only (D-059). `APP_API_KEY` is sent on ingest and delete only (D-060).
- **Sessions.** One session id per browser session and a "New conversation" button.
- **Packaging.** The API image builds (production dependencies only, no Streamlit). The UI service is behind the compose profile `ui` and has its own Dockerfile.
- **Tests.** Added contract tests for all four statuses over HTTP (Groq 429 and timeouts, Voyage 429, empty database), end-to-end API tests, UI page tests, packaging and README checks (the curl examples are checked by tests).
- **README.** Architecture diagram (Mermaid text plus `docs/architecture.png`), setup, API examples, evaluation results, known limitations, next steps.

### Key decisions and why
| Decision | Why |
| --- | --- |
| UI talks to the API over HTTP | Reuses the same path projects 3 and 6 will use |
| UI port 8510, compose profile `ui` | Avoids ports used by other local containers; plain `docker compose up` is unchanged |
| Plain-text rendering of model output | Text from documents and models is data, not markup |

### What was verified
- 787 tests pass, ruff clean.
- Owner-reported: the page was run with `streamlit run` and tested in a browser on Nov 17, 2026; the API image builds (524 MB); the UI image builds and reports Streamlit 1.66.0.

### What to watch out for
- Not verified: the UI image has never been started as a container, and it installs Streamlit with pip, so it does not follow `uv.lock`.
- The screenshots and the final demo script are Milestone 7.
- `/chat` and `/documents` are open (local showcase).
- Whether the README architecture diagram image matches the final code has not been re-checked after the fix pass (the pipeline gained the guard and the original-wording search).

**Update, Nov 19, 2026 (owner-reported).** The UI image was started as a container (`docker compose --profile ui up -d --no-build ui`) and the page, served on port 8510, answered, refused and asked a clarifying question against the API container; two screenshots from that run are in `docs/media/`. Still not verified: the UI image's dependencies come from pip rather than `uv.lock`.

## Milestone 7: Demo

### What this milestone does
Milestone 7 makes the project showable: a redrawn architecture diagram, final screenshots, a demo script and video, a case study, production notes, and a check of how the README looks on GitHub.

### What was built
- **Diagram.** `docs/architecture.png` was redrawn to match the final pipeline (D-089), with a Mermaid text version in the README.
- **Screenshots.** Six images in `docs/media/` (documents panel, answer with a source, refusal, clarifying question, follow-up with memory, injection refused), listed in `docs/media/README.md`.
- **Demo.** `docs/DEMO_SCRIPT.md` with the real evaluation numbers, and a 77-second silent screen recording (`docs/media/demo.mp4`, about 2 MB, H.264, no audio) linked from the README. The 22 MB original stays local.
- **Case study.** `docs/CASE_STUDY.md`: the problem, design, measured results and their limits. The free tiers are framed as a deliberate choice, and paid models for a client are stated as unmeasured.
- **README.** Production notes, a corrected access-control limitation (`/ingest` and `DELETE /documents/{id}` need the `X-API-Key`; `/chat` and listing are open), author-voice wording, and a Quick start that states what was checked. `eval/README.md` points to the real results.

### Key decisions and why
| Decision | Why |
| --- | --- |
| Silent video, no captions | The README, screenshots and case study carry the explanation |
| Small mp4 in the repo, original .mov local | The repo stays small; on GitHub the video downloads instead of playing inline (checked Nov 19, 2026) |

### What was verified
- 791 tests pass, ruff clean.
- Owner-reported (Nov 19, 2026): the README renders on GitHub (diagram, Mermaid, six screenshots). The video was recorded in the container UI and its frames were checked for tabs, keys and personal data.

### What to watch out for
- On GitHub the demo video downloads instead of playing inline (checked Nov 19, 2026); the direct link in the README works that way.
- Not verified: the UI image's dependencies come from pip, not `uv.lock`.
- The results are not a held-out estimate, and the retrieval, correctness and refusal targets are not met.
- Open small items: `/health` does not report a failed clarify step; two judge calls still fail with `reply_cut_off`; there is no held-out question set.
