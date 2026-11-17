# Clarify check

```
CLARIFY CHECK (the clarify step only: no embedding, no search, no answer, no judge)
  run id             : clarify-check__clarify_v1
  rewrite model      : openai/gpt-oss-20b
  clarify prompt     : clarify_v1
  tuning set         : clarify_questions.jsonl (DRAFT, not approved by the owner)
  estimate           : about 60 calls and about 44,900 reserved tokens (real use is lower), about 7 minutes (paced by the tokens-per-minute limit)
  note               : the rewrite model's daily token budget is shared with every other use of it today
```

Tuning check of the clarify step only. The tuning set is a DRAFT written by Claude (not approved by the owner) and is NOT the evaluation set. These numbers are not evaluation results and must not be quoted as such.

| Measure | Count |
| --- | --- |
| Clear questions wrongly clarified (should search) | 4 of 36 |
| Vague questions caught (clarified) | 15 of 16 |
| Vague questions wrongly searched | 1 of 16 |
| Follow-ups handled as expected | 7 of 8 |
| Items with a model or format error | 0 of 60 |

## Every miss

- S09 (clear): expected search, got clarify: 'how do i get to u by bus' -> asked 'Which address are you trying to reach by bus?'
- S10 (clear): expected search, got clarify: 'your phone number?' -> asked 'Which phone number would you like to know?'
- S12 (clear): expected search, got clarify: 'are yuo open on sundays' -> asked 'Do you have any appointments available on Sundays?'
- S24 (clear): expected search, got clarify: 'can i pay in installments' -> asked 'Which treatment or service would you like to pay for in installments?'
- V05 (vague): expected clarify, got search: 'does she work on fridays?'
- F07 (follow-up): expected clarify, got search: 'can i book him'

## Every item

- S01 ok: expected search, got search
- S02 ok: expected search, got search
- S03 ok: expected search, got search
- S04 ok: expected search, got search
- S05 ok: expected search, got search
- S06 ok: expected search, got search
- S07 ok: expected search, got search
- S08 ok: expected search, got search
- S09 MISS: expected search, got clarify
- S10 MISS: expected search, got clarify
- S11 ok: expected search, got search
- S12 MISS: expected search, got clarify
- S13 ok: expected search, got search
- S14 ok: expected search, got search
- S15 ok: expected search, got search
- S16 ok: expected search, got search
- S17 ok: expected search, got search
- S18 ok: expected search, got search
- S19 ok: expected search, got search
- S20 ok: expected search, got search
- S21 ok: expected search, got search
- S22 ok: expected search, got search
- S23 ok: expected search, got search
- S24 MISS: expected search, got clarify
- S25 ok: expected search, got search
- S26 ok: expected search, got search
- S27 ok: expected search, got search
- S28 ok: expected search, got search
- S29 ok: expected search, got search
- S30 ok: expected search, got search
- S31 ok: expected search, got search
- S32 ok: expected search, got search
- S33 ok: expected search, got search
- S34 ok: expected search, got search
- S35 ok: expected search, got search
- S36 ok: expected search, got search
- V01 ok: expected clarify, got clarify
- V02 ok: expected clarify, got clarify
- V03 ok: expected clarify, got clarify
- V04 ok: expected clarify, got clarify
- V05 MISS: expected clarify, got search
- V06 ok: expected clarify, got clarify
- V07 ok: expected clarify, got clarify
- V08 ok: expected clarify, got clarify
- V09 ok: expected clarify, got clarify
- V10 ok: expected clarify, got clarify
- V11 ok: expected clarify, got clarify
- V12 ok: expected clarify, got clarify
- V13 ok: expected clarify, got clarify
- V14 ok: expected clarify, got clarify
- V15 ok: expected clarify, got clarify
- V16 ok: expected clarify, got clarify
- F01 ok: expected search, got search
- F02 ok: expected search, got search
- F03 ok: expected search, got search
- F04 ok: expected search, got search
- F05 ok: expected search, got search
- F06 ok: expected clarify, got clarify
- F07 MISS: expected clarify, got search
- F08 ok: expected clarify, got clarify
