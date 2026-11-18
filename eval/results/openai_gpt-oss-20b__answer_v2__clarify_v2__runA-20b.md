# Evaluation summary

```
Evaluation run openai_gpt-oss-20b__answer_v2__clarify_v2__runA-20b
  answer model       : openai/gpt-oss-20b (prompt answer_v2)
  answer fallback    : openai/gpt-oss-120b
  rewrite model      : openai/gpt-oss-120b (clarify prompt clarify_v2, on)
  judge model        : qwen/qwen3.8-27b (prompts {'correctness': 'judge_correctness_v1', 'faithfulness': 'judge_faithfulness_v2', 'combined': 'judge_combined_v2'})
  similarity threshold: 0.4  top_k: 5
  embedding model    : voyage-4
  questions          : questions.jsonl sha256 5a741ae531a1  APPROVED by the owner
  repeat 1, judge calls per answer 2
  Milestone 5 plan: run the evaluation on BOTH gpt-oss-20b and gpt-oss-120b and choose from the numbers.
```

Run 1 of 1. Questions scored: 40.

| Metric | Value | n | Target | Met |
| --- | --- | --- | --- | --- |
| Retrieval hit rate (all expected sources in top-k) | 69.0% | 29 | 90% | NO |
| Retrieval hit rate (any expected source) | 72.4% | 29 |  | n/a |
| Faithfulness | 100.0% | 13 | 90% | yes |
| Answer correctness | 39.3% | 28 | 85% | NO |
| Refusal accuracy | 69.4% | 36 | 95% | NO |
| Status accuracy (no target yet) | 55.0% | 40 |  | n/a |
| Latency p50 (ms) | 1605.5 | 40 | under 3000 | yes |

Latency p95 2618 ms, max 4362 ms (chat call only; pacing sleeps excluded).

Status accuracy by category: answerable 44.8% (n=29), out_of_scope 71.4% (n=7), vague 100.0% (n=4)

Unjudged answers: correctness ['E05'], faithfulness none.
Errors: none. Answerable questions that got a clarify: ['E08', 'E16', 'E20', 'E22', 'E23', 'E24', 'E26'].
Follow-ups whose setup turn was not answered (`setup_not_answered`; scored as run, judge them by hand): none.

Where a score is close to its target, repeat the run and report the spread. The judge is an LLM; the owner hand-checks about 10 verdicts (see the hand-check file).
