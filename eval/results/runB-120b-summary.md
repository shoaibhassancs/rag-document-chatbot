# runB-120b: per-question summary

Derived from `eval/results/openai_gpt-oss-120b__answer_v2__clarify_v2__runB-120b.jsonl` (git-ignored raw record). Same pipeline as runA2 (gate 0.33, P1a, P1b, clarify guard); only the answer model differs. No answers or question text here: only ids, statuses and numbers. Answer model gpt-oss-120b (answer_v2), clarify gpt-oss-120b (clarify_v2), judge qwen/qwen3.8-27b (judge_correctness_v1, judge_faithfulness_v2), top_k 5, one run. Judge scores are as the judge produced them (after the D-085 re-judging); the owner's review is in `M5-comparison.md`.

## Metrics as printed by the run

| Metric | Value | n | Target | Met |
| --- | --- | --- | --- | --- |
| Retrieval hit rate (all expected sources in top-k) | 82.8% | 29 | 90% | NO |
| Retrieval hit rate (any expected source) | 89.7% | 29 |  | n/a |
| Faithfulness | 100.0% | 23 | 90% | yes |
| Answer correctness | 71.4% | 28 | 85% | NO |
| Refusal accuracy | 83.3% | 36 | 95% | NO |
| Status accuracy (no target yet) | 75.0% | 40 |  | n/a |
| Latency p50 (ms) | 2225.0 | 40 | under 3000 | yes |

Unjudged: correctness of E21 (judge call cut off, `reply_cut_off`).

## Per-question table

| qid | category | expected status | got status | clarify action | guard fired | top-1 similarity | expected sources in top 5 | judge correct | judge faithful |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E01 | answerable | answered | answered | search |  | 0.4846 | all | 1 | 1 |
| E02 | answerable | answered | answered | search |  | 0.3837 | all | 1 | 1 |
| E03 | answerable | answered | answered | search |  | 0.3740 | all | 1 | 1 |
| E04 | answerable | answered | refused **(differs)** | search |  | 0.3396 | all |  |  |
| E05 | answerable | answered | answered | search |  | 0.5129 | all | 1 | 1 |
| E06 | answerable | answered | refused **(differs)** | search |  | 0.1644 | all |  |  |
| E07 | answerable | answered | answered | search |  | 0.6505 | all | 1 | 1 |
| E08 | answerable | answered | clarify **(differs)** | clarify |  |  |  |  |  |
| E09 | answerable | answered | answered | search |  | 0.5593 | all | 1 | 1 |
| E10 | answerable | answered | answered | search |  | 0.5473 | all | 1 | 1 |
| E11 | answerable | answered | answered | search |  | 0.5607 | all | 1 | 1 |
| E12 | answerable | answered | answered | search |  | 0.5453 | all | 1 | 1 |
| E13 | answerable | answered | answered | search |  | 0.5002 | all | 1 | 1 |
| E14 | answerable | answered | answered | search |  | 0.6292 | all | 1 | 1 |
| E15 | answerable | answered | answered | search |  | 0.4081 | all | 1 | 1 |
| E16 | answerable | answered | refused **(differs)** | search | yes | 0.6125 | all |  |  |
| E17 | answerable | answered | refused **(differs)** | search |  | 0.3434 | none |  |  |
| E18 | answerable | answered | answered | search |  | 0.3827 | all | 1 | 1 |
| E19 | answerable | answered | answered | search |  | 0.3873 | all | 1 | 1 |
| E20 | answerable | answered | answered | search | yes | 0.4795 | all | 0 | 1 |
| E21 | answerable | answered | answered | search |  | 0.5049 | some | unjudged | 1 |
| E22 | answerable | answered | answered | search | yes | 0.5827 | some | 1 | 1 |
| E23 | answerable | answered | answered | search |  | 0.6238 | all | 1 | 1 |
| E24 | answerable | answered | answered | search | yes | 0.6778 | all | 1 | 1 |
| E25 | answerable | answered | answered | search |  | 0.5126 | all | 1 | 1 |
| E26 | answerable | answered | clarify **(differs)** | clarify |  |  |  |  |  |
| E27 | answerable | answered | answered | search |  | 0.5651 | all | 1 | 1 |
| E28 | answerable | answered | answered | search |  | 0.5756 | all | 1 | 1 |
| E29 | out_of_scope | refused | refused | search |  | 0.5366 |  |  |  |
| E30 | out_of_scope | refused | clarify **(differs)** | clarify |  |  |  |  |  |
| E31 | answerable | answered | answered | search |  | 0.5352 | all | 0 | 1 |
| E32 | out_of_scope | refused | refused | search |  | 0.5533 |  |  |  |
| E33 | out_of_scope | refused | clarify **(differs)** | clarify |  |  |  |  |  |
| E34 | out_of_scope | refused | refused | search |  | 0.3639 |  |  |  |
| E35 | out_of_scope | refused | refused | search |  | 0.5102 |  |  |  |
| E36 | out_of_scope | refused | refused | search |  | 0.3995 |  |  |  |
| E37 | vague | clarify | refused **(differs)** | search | yes | 0.4766 |  |  |  |
| E38 | vague | clarify | clarify | clarify |  |  |  |  |  |
| E39 | vague | clarify | refused **(differs)** | search | yes | 0.5607 |  |  |  |
| E40 | vague | clarify | clarify | clarify |  |  |  |  |  |
