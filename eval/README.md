# Evaluation (Milestone 5): harness and rubric

**The question set was approved by the owner on Nov 17, 2026** (all 40 read in a table with the reviewer; mix 29 answerable, 7 out of scope, 4 vague). The harness is built and tested with mocks; it has not run live.

**No score exists yet.** No evaluation has been run. No number may appear in this README, the main README, the case study or a screenshot unless it came from a real run on questions the owner has approved. Anything labelled `DRY RUN: FAKE DATA, NOT A RESULT` is produced by fakes and is never a result.

## What it measures

The evaluation answers the client question "how do you know it works?" with numbers from a fixed question set. Targets are from `SPEC.md`.

| Metric | Definition | Target |
| --- | --- | --- |
| Retrieval hit rate | Answerable questions: ALL expected source files are among the top-k retrieved chunks (before the similarity threshold). The report also shows "any expected source". Headline is "all" (stricter). PROVISIONAL | 90% or higher |
| Faithfulness | Judged answers: the answer only uses facts from the chunks it was based on (see "Faithfulness basis") | 90% or higher |
| Answer correctness | ALL answerable questions: the answer matches the staff-written expected answer. A refused, clarified or errored answerable question counts as 0 | 85% or higher |
| Refusal accuracy | Out-of-scope questions are refused and answerable ones are not refused (vague questions are not part of this metric) | 95% or higher |
| Status accuracy | The `/chat` status equals the expected status (`answered`, `refused`, `clarify`), overall and per category | reported; target set after the baseline |
| Latency | Median (p50), p95 and max of the `/chat` call. Pacing sleeps are not included; rate-limit waits inside the call are | p50 under 3 s |

Also reported: errors, answerable questions that got a `clarify` (over-asking), unjudged answers, and the spread over repeated runs.

## The question set (approved Nov 17, 2026)

`eval/questions.jsonl`, JSON Lines. The first line is a header whose `_header` starts with "APPROVED by the owner, Nov 17, 2026". Every question has `"status": "APPROVED"`. If the header starts with DRAFT, or any question is back at `"status": "DRAFT"`, the runner treats the whole file as DRAFT (a live run then also needs `--allow-draft`, and the summary says the numbers must not be quoted). Changing a question means setting it back to DRAFT until the owner approves it again. Fields: `id`, `category` (`answerable`, `out_of_scope`, `vague`), `question`, `expected_sources` (file names in `data/sample_docs/`), `expected_answer` (short), `expected_status`, `status`, optional `setup` (a first message sent in the same session before the scored question, for follow-ups) and `note`.

- 40 scored questions: 29 answerable, 7 out of scope, 4 vague. Four of the answerable ones are two-turn follow-ups ("and saturdays?"); their setup turn is sent first and is not scored.
- It is separate from the calibration set (`data/calibration/`): no shared questions or wording (a test checks it).
- Every expected answer was copied from or checked line by line against the sample documents. The owner read and approved them on Nov 17, 2026.
- A `setup` turn that returns `clarify` or `error` flags the follow-up as `setup_not_answered`. It is still scored as it ran, and the summary lists these separately for the owner to judge.

## The judge

An LLM judge (`qwen/qwen3.8-27b`, a different family from the answerer) grades only answered answerable questions: one call for correctness and one for faithfulness (`--judge-calls 1` merges them). Prompts are versioned files: `prompts/judge_correctness_v1.txt`, `prompts/judge_faithfulness_v2.txt` and `prompts/judge_combined_v2.txt` are the defaults (the v1 faithfulness and combined files are kept; choose a version with `JUDGE_FAITHFULNESS_PROMPT_VERSION` and `JUDGE_COMBINED_PROMPT_VERSION`). v2 says the clinic is called BrightSmile Dental and that using that name, "we", "our", "the clinic", "the team" or "the practice" is never a claim to check; every other claim is still checked as strictly as in v1 (Nov 17, 2026: the first smoke run's judge marked the clinic's name as unsupported). The versions used appear in the banner and at the top of the hand-check file. The judge returns JSON with a 0 or 1 score and a one-sentence reason.

- The judge's input (question, answers, chunks) is data, not instructions.
- A bad reply is retried once. A second failure is recorded as `judge_error`, excluded from the denominator and listed as unjudged. It is never turned into a 0 or a 1.
- Everything else (status, refusal, hit rate, latency) is scored by code, not by the judge.
- **Hand-check:** the report writes `<run>-handcheck.md` with 10 judged verdicts chosen with a fixed seed. The owner checks about 10 verdicts by hand before trusting the judge.

### Faithfulness basis (limitation)

Faithfulness is judged against every chunk that was placed in the answer prompt (`context_chunks`, owner decision D-080, Nov 17, 2026): the top-5 retrieved chunks whose similarity passed the 0.40 threshold. It is not limited to `used_chunk_ids`, the chunks the model says it used, which stays in each record unchanged for inspection: a correct answer must not fail because the model pointed at a neighbouring chunk of the same file. The basis and the judged chunk ids are recorded per answer and shown in the hand-check file. **Limitation:** an answer is accepted if its facts appear anywhere in the prompt, even in a chunk the model did not cite; a state file written under the earlier basis (`used_chunks`) refuses to resume.

## Before a full run: check the clarify step

On Nov 17, 2026 the first 3-question live smoke (E01 to E03) showed that `clarify_v1` stopped two clear questions about the one clinic with "Which location are you referring to?" (E01 and E02). The same smoke showed the faithfulness judge marking the clinic's own name as an unsupported claim on E03; `judge_faithfulness_v2` fixes that. The smoke is not a result and is never quoted.

`scripts/check_clarify.py` runs ONLY the clarify step (no embedding, no search, no answer, no judge) on a separate tuning set, `data/calibration/clarify_questions.jsonl` (60 items written by Claude, DRAFT until the owner approves it, none of them repeating an evaluation or threshold-calibration question; a test checks that). It prints the questions wrongly clarified, the vague ones wrongly searched, the follow-ups and every miss, and writes `eval/results/clarify-check__<prompt version>.md`.

```
uv run python -m scripts.check_clarify --dry-run
uv run python -m scripts.check_clarify --confirm-live --prompt-version clarify_v2
```

`--set tuning|heldout` chooses the item set (default tuning). The held-out set, `data/calibration/clarify_heldout.jsonl` (30 new DRAFT items, never used to edit a prompt), is run once at the end for the final decision; its banner and results say so, and its files end in `__heldout`. On Nov 17, 2026 the tuning check gave `clarify_v1` 4 of 36 clear questions wrongly clarified and `clarify_v2` 6 of 36 (a new family: questions that name an insurer, card or payment arrangement), so `clarify_v3` was written.

A live check needs `--confirm-live` and makes about 60 calls (about 45,000 reserved tokens, about 7 minutes at the free-tier pace; resumable by running the same command). **The app's clarify call runs on `openai/gpt-oss-120b` with prompt `clarify_v2`** (owner decision D-078, Nov 17, 2026; set by `REWRITE_LLM_MODEL` and `CLARIFY_PROMPT_VERSION`). Known limitation: it over-asks on some follow-up questions (7 of 12 follow-ups right in the practice checks, which are not the evaluation).

## Running it

```
uv run python -m eval.run_eval --dry-run
uv run python -m eval.run_eval --confirm-live [--phase answer|judge|all] [--repeat N] [--judge-calls 1|2] [--ids E01,E02] [--limit N]
```

- `--dry-run` uses fakes (no network, no database), writes only to a temporary directory, never to `eval/results/`, and every output starts with `DRY RUN: FAKE DATA, NOT A RESULT`.
- A live run always needs `--confirm-live`. It needs `--allow-draft` as well only if the file has any DRAFT header or question (the check covers the whole file, even with `--ids` or `--limit`). It refuses to start if a needed setting has no value (the message names only the setting, never a value) and it never prints, logs or writes a key.
- At startup it prints, and writes into the results header: the run id, the active answer model and prompt version, the fallback model, the rewrite model and clarify prompt version, the judge model and prompt versions, the similarity threshold, the embedding model, the question file hash and whether it is DRAFT or APPROVED. It also prints the Milestone 5 reminder: run the evaluation on BOTH `gpt-oss-20b` and `gpt-oss-120b` (set `ANSWER_LLM_MODEL` in `.env` for each run, and `ANSWER_FALLBACK_MODEL` to the other model) and choose from the numbers.
- **Resumable.** Progress is saved after every unit in `eval/.state/<run_id>.jsonl` (git-ignored). Run the same command again to continue, across days. A different model, prompt, threshold or question file refuses to resume unless `--force-config`; `--fresh` sets the old state aside and starts over.
- **A run stops** (state saved) if any call falls back to another model, hits a long rate limit (a daily cap) or the daily budget would be exceeded, so one result never mixes two models.
- Output: `eval/results/<run_id>.jsonl` (raw per-question rows, git-ignored), `eval/results/<run_id>.md` (the summary table; tracked, so before and after tables can be quoted), `eval/results/<run_id>-handcheck.md` (git-ignored).
- The runner uses the real `ChatService` with the real database. Its sessions are named `eval-<run_id>-<question id>-r<run>`, so its rows in `chat_logs` are easy to tell apart from real use.

## Quota and Voyage note

Groq's free plan allows 30 requests per minute, 8,000 tokens per minute, 1,000 requests per day and 200,000 tokens per day, per model. The runner paces itself at 90% of each limit and reserves each call's prompt estimate plus its output cap, the same way Groq does at admission.

- The clarify call runs on the 120b (about 800 reserved tokens, about 950 used) and **shares the 120b's 200,000 daily tokens with the 120b answers**. With the 20b as answer model, the 20b serves only the answers (about 70,000 to 85,000 tokens for 44 chat calls) and the 120b only the clarify calls (about 42,000). With the 120b as answer model, the 120b serves both (about 120,000 to 130,000). A full pass of one model is about one day of that model's token budget. About two questions a minute (Voyage's 3 requests a minute is the limit). Run A (20b answers) and Run B (120b answers) should be on separate days unless the Groq console shows enough room.
- The judge is on its own model (`qwen/qwen3.8-27b`) with its own quota: about 58 calls for 29 answers, roughly half of its daily budget.
- Voyage (3 requests and 10,000 tokens per minute) is used for one tiny query embedding per question; the existing limiter and query cache apply, and the pace keeps it under 3 requests a minute.
- These are estimates, not measurements.

## What stays with the owner

Approving or editing the questions and expected answers; any live run; baseline numbers; chunk-size and threshold tuning; choosing the answer model; hand-checking about 10 judge verdicts.
