# Clarify check

```
CLARIFY CHECK (the clarify step only: no embedding, no search, no answer, no judge)
  run id             : clarify-check__clarify_v2__extra
  rewrite model      : openai/gpt-oss-120b
  clarify prompt     : clarify_v2
  item set           : extra (clarify_extra.jsonl; DRAFT, not approved)
  EXTRA SET (DRAFT, informed by Run A): measures the clarify guard (P2) on and off; not an evaluation result
  estimate           : about 16 calls and about 18,482 reserved tokens (real use is lower), about 3 minutes (paced by the tokens-per-minute limit)
  note               : the rewrite model's daily token budget is shared with every other use of it today
```

Tuning check of the clarify step only. The tuning set is a DRAFT written by Claude (not approved by the owner) and is NOT the evaluation set. These numbers are not evaluation results and must not be quoted as such.

| Measure | Count |
| --- | --- |
| Clear questions wrongly clarified (should search) | 2 of 12 |
| Vague questions caught (clarified) | 0 of 4 |
| Vague questions wrongly searched | 4 of 4 |
| Follow-ups handled as expected | 0 of 0 |
| Items with a model or format error | 0 of 16 |

## Every miss

- X04 (clear): expected search, got clarify: "I have a root canal booked for next week and I'm really nervous, can I take something to calm down beforehand?" -> asked 'Which medication are you considering for calming down?'
- X10 (clear): expected search, got clarify: 'We are a family of four and want check-ups on the same morning, can you book us back to back?' -> asked 'Which dentist would you like each family member to see?'
- X13 (vague): expected clarify, got search: 'Does the treatment hurt a lot?'
- X14 (vague): expected clarify, got search: 'What do I need to do before the procedure?'
- X15 (vague): expected clarify, got search: 'Is my dentist available on Saturday?'
- X16 (vague): expected clarify, got search: 'How long should I wait after the appointment before eating?'

## The clarify guard (P2) on and off

The table above is WITH the guard (the app default). Without it:

| Measure | Guard on | Guard off |
| --- | --- | --- |
| Clear wrongly clarified | 2 of 12 | 2 of 12 |
| Vague questions caught | 0 of 4 | 2 of 4 |
| Follow-ups handled as expected | 0 of 0 | 0 of 0 |

Clarifies the guard turned into searches: 2.
- X13 (vague, expected clarify): terms ['treatment'] already named in 'Does the treatment hurt a lot?'
- X14 (vague, expected clarify): terms ['procedure'] already named in 'What do I need to do before the procedure?'

## Every item

- X01 ok: expected search, got search
- X02 ok: expected search, got search
- X03 ok: expected search, got search
- X04 MISS: expected search, got clarify
- X05 ok: expected search, got search
- X06 ok: expected search, got search
- X07 ok: expected search, got search
- X08 ok: expected search, got search
- X09 ok: expected search, got search
- X10 MISS: expected search, got clarify
- X11 ok: expected search, got search
- X12 ok: expected search, got search
- X13 MISS: expected clarify, got search
- X14 MISS: expected clarify, got search
- X15 MISS: expected clarify, got search
- X16 MISS: expected clarify, got search
