# Baseline (Run A): per-question summary

Derived from `eval/results/openai_gpt-oss-20b__answer_v2__clarify_v2__runA-20b.jsonl` (git-ignored raw record). No answers or question text here: only ids and statuses. Run A: answer model gpt-oss-20b (answer_v2), clarify gpt-oss-120b (clarify_v2), gate 0.40, top_k 5, judge qwen/qwen3.8-27b. This is the BASELINE before the INSTRUCTIONS_27 fix pass; the judge verdicts had not yet been hand-checked. The summary table of metrics is in `openai_gpt-oss-20b__answer_v2__clarify_v2__runA-20b.md`.

| qid | category | expected status | got status | clarify action | top-1 similarity |
| --- | --- | --- | --- | --- | --- |
| E01 | answerable | answered | answered | search | 0.5468 |
| E02 | answerable | answered | answered | search | 0.4771 |
| E03 | answerable | answered | refused **(differs)** | search | 0.3597 |
| E04 | answerable | answered | refused **(differs)** | search | 0.3396 |
| E05 | answerable | answered | answered | search | 0.4657 |
| E06 | answerable | answered | refused **(differs)** | search | 0.3605 |
| E07 | answerable | answered | answered | search | 0.6332 |
| E08 | answerable | answered | clarify **(differs)** | clarify |  |
| E09 | answerable | answered | answered | search | 0.4335 |
| E10 | answerable | answered | answered | search | 0.5019 |
| E11 | answerable | answered | answered | search | 0.5390 |
| E12 | answerable | answered | answered | search | 0.5224 |
| E13 | answerable | answered | answered | search | 0.5035 |
| E14 | answerable | answered | answered | search | 0.6289 |
| E15 | answerable | answered | refused **(differs)** | search | 0.3590 |
| E16 | answerable | answered | clarify **(differs)** | clarify |  |
| E17 | answerable | answered | refused **(differs)** | search | 0.4542 |
| E18 | answerable | answered | refused **(differs)** | search | 0.3481 |
| E19 | answerable | answered | refused **(differs)** | search | 0.3367 |
| E20 | answerable | answered | clarify **(differs)** | clarify |  |
| E21 | answerable | answered | answered | search | 0.5309 |
| E22 | answerable | answered | clarify **(differs)** | clarify |  |
| E23 | answerable | answered | clarify **(differs)** | clarify |  |
| E24 | answerable | answered | clarify **(differs)** | clarify |  |
| E25 | answerable | answered | answered | search | 0.5126 |
| E26 | answerable | answered | clarify **(differs)** | clarify |  |
| E27 | answerable | answered | refused **(differs)** | search | 0.4100 |
| E28 | answerable | answered | answered | search | 0.5765 |
| E29 | out_of_scope | refused | refused | search | 0.4905 |
| E30 | out_of_scope | refused | refused | search | 0.4844 |
| E31 | answerable | answered | refused **(differs)** | search | 0.3891 |
| E32 | out_of_scope | refused | answered **(differs)** | search | 0.4526 |
| E33 | out_of_scope | refused | clarify **(differs)** | clarify |  |
| E34 | out_of_scope | refused | refused | search | 0.4536 |
| E35 | out_of_scope | refused | refused | search | 0.5327 |
| E36 | out_of_scope | refused | refused | search | 0.3643 |
| E37 | vague | clarify | clarify | clarify |  |
| E38 | vague | clarify | clarify | clarify |  |
| E39 | vague | clarify | clarify | clarify |  |
| E40 | vague | clarify | clarify | clarify |  |
