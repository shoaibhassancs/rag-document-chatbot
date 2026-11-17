# Clarify check

```
CLARIFY CHECK (the clarify step only: no embedding, no search, no answer, no judge)
  run id             : clarify-check__clarify_v2__heldout__gpt-oss-120b-heldout
  rewrite model      : openai/gpt-oss-120b
  clarify prompt     : clarify_v2
  item set           : heldout (clarify_heldout.jsonl; DRAFT, not approved)
  HELD-OUT SET: use for the final decision only; never edit a prompt after looking at these results
  estimate           : about 30 calls and about 34,344 reserved tokens (real use is lower), about 5 minutes (paced by the tokens-per-minute limit)
  note               : the rewrite model's daily token budget is shared with every other use of it today
```

Tuning check of the clarify step only. The tuning set is a DRAFT written by Claude (not approved by the owner) and is NOT the evaluation set. These numbers are not evaluation results and must not be quoted as such.

| Measure | Count |
| --- | --- |
| Clear questions wrongly clarified (should search) | 0 of 18 |
| Vague questions caught (clarified) | 8 of 8 |
| Vague questions wrongly searched | 0 of 8 |
| Follow-ups handled as expected | 1 of 4 |
| Items with a model or format error | 0 of 30 |

## Every miss

- HF01 (follow-up): expected search, got clarify: 'and does my insurance cover it?' -> asked 'Which insurance plan do you have?'
- HF02 (follow-up): expected search, got clarify: 'how long does it last' -> asked 'Which whitening treatment are you referring to?'
- HF04 (follow-up): expected search, got clarify: 'will i be charged for it?' -> asked 'Which appointment are you referring to?'

## Every item

- HS01 ok: expected search, got search
- HS02 ok: expected search, got search
- HS03 ok: expected search, got search
- HS04 ok: expected search, got search
- HS05 ok: expected search, got search
- HS06 ok: expected search, got search
- HS07 ok: expected search, got search
- HS08 ok: expected search, got search
- HS09 ok: expected search, got search
- HS10 ok: expected search, got search
- HS11 ok: expected search, got search
- HS12 ok: expected search, got search
- HS13 ok: expected search, got search
- HS14 ok: expected search, got search
- HS15 ok: expected search, got search
- HS16 ok: expected search, got search
- HS17 ok: expected search, got search
- HS18 ok: expected search, got search
- HV01 ok: expected clarify, got clarify
- HV02 ok: expected clarify, got clarify
- HV03 ok: expected clarify, got clarify
- HV04 ok: expected clarify, got clarify
- HV05 ok: expected clarify, got clarify
- HV06 ok: expected clarify, got clarify
- HV07 ok: expected clarify, got clarify
- HV08 ok: expected clarify, got clarify
- HF01 MISS: expected search, got clarify
- HF02 MISS: expected search, got clarify
- HF03 ok: expected clarify, got clarify
- HF04 MISS: expected search, got clarify
