# Clarify check

```
CLARIFY CHECK (the clarify step only: no embedding, no search, no answer, no judge)
  run id             : clarify-check__clarify_v2__gpt-oss-120b
  rewrite model      : openai/gpt-oss-120b
  clarify prompt     : clarify_v2
  item set           : tuning (clarify_questions.jsonl; DRAFT, not approved)
  estimate           : about 60 calls and about 68,720 reserved tokens (real use is lower), about 10 minutes (paced by the tokens-per-minute limit)
  note               : the rewrite model's daily token budget is shared with every other use of it today
```

Tuning check of the clarify step only. The tuning set is a DRAFT written by Claude (not approved by the owner) and is NOT the evaluation set. These numbers are not evaluation results and must not be quoted as such.

| Measure | Count |
| --- | --- |
| Clear questions wrongly clarified (should search) | 1 of 36 |
| Vague questions caught (clarified) | 16 of 16 |
| Vague questions wrongly searched | 0 of 16 |
| Follow-ups handled as expected | 6 of 8 |
| Items with a model or format error | 0 of 60 |

## Every miss

- S36 (clear): expected search, got clarify: 'can you refer me to a specialist if i need one' -> asked 'Which type of specialist are you asking about?'
- F03 (follow-up): expected search, got clarify: 'and does insurance cover it?' -> asked 'Which insurance plan are you referring to?'
- F05 (follow-up): expected search, got clarify: 'which one is better for a child' -> asked 'Are you asking about nitrous oxide or oral sedation for your child?'

## Every item

- S01 ok: expected search, got search
- S02 ok: expected search, got search
- S03 ok: expected search, got search
- S04 ok: expected search, got search
- S05 ok: expected search, got search
- S06 ok: expected search, got search
- S07 ok: expected search, got search
- S08 ok: expected search, got search
- S09 ok: expected search, got search
- S10 ok: expected search, got search
- S11 ok: expected search, got search
- S12 ok: expected search, got search
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
- S24 ok: expected search, got search
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
- S36 MISS: expected search, got clarify
- V01 ok: expected clarify, got clarify
- V02 ok: expected clarify, got clarify
- V03 ok: expected clarify, got clarify
- V04 ok: expected clarify, got clarify
- V05 ok: expected clarify, got clarify
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
- F03 MISS: expected search, got clarify
- F04 ok: expected search, got search
- F05 MISS: expected search, got clarify
- F06 ok: expected clarify, got clarify
- F07 ok: expected clarify, got clarify
- F08 ok: expected clarify, got clarify
