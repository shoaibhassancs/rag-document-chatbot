# Clarify check

```
CLARIFY CHECK (the clarify step only: no embedding, no search, no answer, no judge)
  run id             : clarify-check__clarify_v2
  rewrite model      : openai/gpt-oss-20b
  clarify prompt     : clarify_v2
  tuning set         : clarify_questions.jsonl (DRAFT, not approved by the owner)
  estimate           : about 60 calls and about 68,720 reserved tokens (real use is lower), about 10 minutes (paced by the tokens-per-minute limit)
  note               : the rewrite model's daily token budget is shared with every other use of it today
```

Tuning check of the clarify step only. The tuning set is a DRAFT written by Claude (not approved by the owner) and is NOT the evaluation set. These numbers are not evaluation results and must not be quoted as such.

| Measure | Count |
| --- | --- |
| Clear questions wrongly clarified (should search) | 6 of 36 |
| Vague questions caught (clarified) | 16 of 16 |
| Vague questions wrongly searched | 0 of 16 |
| Follow-ups handled as expected | 7 of 8 |
| Items with a model or format error | 0 of 60 |

## Every miss

- S15 (clear): expected search, got clarify: 'do you accept Aetna?' -> asked 'Which treatment are you referring to?'
- S17 (clear): expected search, got clarify: 'can I use my FSA card to pay' -> asked 'Which treatment would you like to pay for with your FSA card?'
- S21 (clear): expected search, got clarify: 'how long do i need to wait before eating after whitening' -> asked 'Which whitening treatment are you referring to?'
- S24 (clear): expected search, got clarify: 'can i pay in installments' -> asked 'Which treatment would you like to pay for in installments?'
- S25 (clear): expected search, got clarify: 'Do you have a payment plan for big treatments?' -> asked 'Which treatment are you referring to?'
- S36 (clear): expected search, got clarify: 'can you refer me to a specialist if i need one' -> asked 'Which type of specialist are you looking for?'
- F03 (follow-up): expected search, got clarify: 'and does insurance cover it?' -> asked 'Which insurance plan are you referring to?'

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
- S15 MISS: expected search, got clarify
- S16 ok: expected search, got search
- S17 MISS: expected search, got clarify
- S18 ok: expected search, got search
- S19 ok: expected search, got search
- S20 ok: expected search, got search
- S21 MISS: expected search, got clarify
- S22 ok: expected search, got search
- S23 ok: expected search, got search
- S24 MISS: expected search, got clarify
- S25 MISS: expected search, got clarify
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
- F05 ok: expected search, got search
- F06 ok: expected clarify, got clarify
- F07 ok: expected clarify, got clarify
- F08 ok: expected clarify, got clarify
