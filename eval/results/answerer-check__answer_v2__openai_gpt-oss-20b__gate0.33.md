# V4: answer model on practice out-of-scope and entity-mismatch questions

```
V4: ANSWER MODEL on practice out-of-scope and entity-mismatch questions
  run id        : answerer-check__answer_v2__openai_gpt-oss-20b__gate0.33
  answer model  : openai/gpt-oss-20b (prompt answer_v2, fallback OFF)
  gate          : 0.33
  questions     : 12 (practice and DRAFT entity items; not the evaluation set)
  estimate      : up to 12 answer calls and about 31,200 reserved tokens on the answer model; 1 Voyage request
  RULE P4: ship a new answer_v3 rule ONLY if at least one entity-mismatch item is ANSWERED.
  RULE gate: if MORE THAN ONE out-of-scope or near-miss question that passes the gate but not 0.40 is ANSWERED, use 0.35 instead of the candidate gate.
```

Practice questions, NOT the evaluation set. Not an evaluation result.

| id | kind | top-1 | outcome | wrong? | answer (first 120 characters) |
| --- | --- | --- | --- | --- | --- |
| O02 | near_miss | 0.570 | model |  |  |
| O04 | near_miss | 0.444 | model |  |  |
| O07 | near_miss | 0.369 | model |  |  |
| O08 | near_miss | 0.485 | model |  |  |
| O09 | near_miss | 0.577 | model |  |  |
| O10 | injection | 0.504 | model |  |  |
| N01 | entity_mismatch | 0.563 | model |  |  |
| N02 | entity_mismatch | 0.526 | model |  |  |
| N03 | entity_mismatch | 0.569 | model |  |  |
| N04 | entity_mismatch | 0.334 | model |  |  |
| N05 | entity_mismatch | 0.651 | model |  |  |
| N06 | entity_mismatch | 0.354 | model |  |  |

Entity-mismatch items answered (should be refused): 0 of 6.
Out-of-scope items answered in total: 0 of 12.
Items that pass gate 0.33 but not 0.40: 3 (O07, N04, N06); of these answered: 0.

RULE P4: ship a new answer_v3 rule ONLY if at least one entity-mismatch item is ANSWERED.
RULE gate: if MORE THAN ONE out-of-scope or near-miss question that passes the gate but not 0.40 is ANSWERED, use 0.35 instead of the candidate gate.

RESULT P4: NOT triggered: no entity-mismatch item was answered. Do not change the prompt.
RESULT gate: not tripped (0 extra answered): the candidate gate stands (the owner approves it).
