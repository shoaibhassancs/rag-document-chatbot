# M5 comparison: Run A, runA2 and runB

Ids, statuses and numbers only. 40 owner-approved questions, one run each. **Run A** = baseline (20b, gate 0.40, before the fix pass). **runA2** = the same 20b after one general fix pass (gate 0.33, P1a, P1b, guard). **runB** = the same fixed pipeline as runA2 with the 120b as answer model. The fixes were chosen after seeing Run A on the same 40 questions, so the A to runA2 change is NOT a held-out estimate. A single run has noise of about one or two questions (the clarify step is not deterministic).

## Scores

| Metric | Target | Run A | runA2 (20b) | runB (120b) |
| --- | --- | --- | --- | --- |
| Retrieval hit rate, all sources | 90% | 69.0% (n=29) | 79.3% (n=29) | 82.8% (n=29) |
| Retrieval hit rate, any source | | 72.4% | 86.2% | 89.7% |
| Faithfulness, judge | 90% | 100% (n=13) | 100% (n=21) | 100% (n=23) |
| Correctness, judge score | 85% | 39.3% (n=28) | 69.0% (20 of 29) | 71.4% (20 of 28; E21 unjudged) |
| Correctness, owner-reviewed | 85% | not recomputed (see note) | 72.4% (21 of 29) | 75.9% (22 of 29) |
| Refusal accuracy | 95% | 69.4% (n=36) | 80.6% (n=36) | 83.3% (n=36) |
| Status accuracy | none | 55.0% | 72.5% | 75.0% |
| Status: answerable | | 44.8% | 75.9% | 79.3% |
| Status: out of scope | | 71.4% | 71.4% | 71.4% |
| Status: vague | | 100.0% | 50.0% | 50.0% |
| Latency p50 / p95 | p50 under 3 s | 1606 / 2618 ms | 2061 / 2878 ms | 2225 / 3480 ms |

Targets met: faithfulness and latency p50. Not met: retrieval hit rate, correctness, refusal accuracy. Vague handling got worse on purpose (the clarify guard, P2; D-083).

**Owner-reviewed correctness** (Nov 19): an answer counts as correct if the owner rates it 7 out of 10 or better. Only the judge's zeros on answered questions and the unjudged items were reviewed, plus three spot-checked passes; so the reviewed scores can only be higher than the judge scores. The judge's passes were not all reviewed. Run A's published numbers stay as the judge produced them: the owner rated Run A's E21 (judge 0) 7 out of 10 on Nov 18, which would raise Run A's correctness too, and it is not recomputed here.

The gap between runA2 and runB is one question on every measure, within run-to-run noise. No claim of a difference between the models.

## Owner review verdicts (Nov 19)

| id | run | judge | owner verdict |
| --- | --- | --- | --- |
| E16 | runA2 | correct 0 | judge's 0 stands (the patient would not have been well served) |
| E31 | runA2 and runB | correct 0 | judge overturned in both runs: good, acceptable, clear (counts as correct) |
| E20 | runB | correct 0 | judge's 0 stands (6 of 10) |
| E21 | runB | call failed | counts as correct (7 of 10) |
| E21 | Run A | correct 0 | owner rated 7 of 10 on Nov 18; published Run A number unchanged |
| E20 | runA2 | faithful: call failed | counts as faithful (1): the reviewer's own check, not the judge's |
| E18, E22, E23 | runA2 | passes | spot-checked: owner agreed on correctness and faithfulness |

## Status changes (got status), ids only

### Run A to runA2

| qid | category | Run A | runA2 | now matches expected |
| --- | --- | --- | --- | --- |
| E03 | answerable | refused | answered | yes |
| E15 | answerable | refused | answered | yes |
| E16 | answerable | clarify | answered | yes |
| E18 | answerable | refused | answered | yes |
| E19 | answerable | refused | answered | yes |
| E20 | answerable | clarify | answered | yes |
| E21 | answerable | answered | refused | no |
| E22 | answerable | clarify | answered | yes |
| E23 | answerable | clarify | answered | yes |
| E24 | answerable | clarify | answered | yes |
| E30 | out_of_scope | refused | clarify | no |
| E31 | answerable | refused | answered | yes |
| E32 | out_of_scope | answered | refused | yes |
| E37 | vague | clarify | refused | no |
| E39 | vague | clarify | refused | no |

### runA2 to runB

| qid | category | runA2 | runB | matches expected in runB |
| --- | --- | --- | --- | --- |
| E16 | answerable | answered | refused | no |
| E21 | answerable | refused | answered | yes |
| E27 | answerable | refused | answered | yes |
