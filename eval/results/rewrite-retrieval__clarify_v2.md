# V1: original wording vs rewrite (practice questions)

```
V1: ORIGINAL WORDING vs REWRITE on the practice calibration questions
  run id        : rewrite-retrieval__clarify_v2
  rewrite model : openai/gpt-oss-120b (clarify prompt clarify_v2)
  questions     : questions.jsonl (28 items; practice, not the evaluation set)
  estimate      : about 28 Groq calls and about 31,978 reserved tokens on the rewrite model, about 5 minutes; 1 Voyage request (originals and rewrites together)
  RULE: switch SEARCH_ORIGINAL_WHEN_NO_HISTORY on only if the ORIGINAL wording keeps ALL expected sources of ALL practice answerables in the top 5.
```

Practice calibration questions, NOT the evaluation set. Not an evaluation result.

| id | kind | clarify | orig top-1 | orig expected rank | rewrite top-1 | rewrite expected rank | orig best expected | rewrite best expected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A01 | answerable | search | 0.617 | insurance-and-billing.pdf:1 | 0.615 | insurance-and-billing.pdf:1 | 0.617 | 0.615 |
| A02 | answerable | search | 0.487 | cancellation-policy.pdf:1 | 0.451 | cancellation-policy.pdf:1 | 0.487 | 0.451 |
| A03 | answerable | search | 0.673 | post-extraction-aftercare.pdf:1 | 0.662 | post-extraction-aftercare.pdf:1 | 0.673 | 0.662 |
| A04 | answerable | search | 0.597 | services-and-prices.pdf:1 | 0.611 | services-and-prices.pdf:1 | 0.597 | 0.611 |
| A05 | answerable | search | 0.545 | holiday-closures-2026.txt:1 opening-hours.md:2 | 0.497 | holiday-closures-2026.txt:1 opening-hours.md:2 | 0.545 | 0.497 |
| A06 | answerable | search | 0.517 | opening-hours.md:1 clinic-website.html:3 | 0.508 | opening-hours.md:1 clinic-website.html:3 | 0.517 | 0.508 |
| A07 | answerable | search | 0.466 | opening-hours.md:1 | 0.513 | opening-hours.md:1 | 0.466 | 0.513 |
| A08 | answerable | search | 0.616 | services-and-prices.pdf:1 | 0.616 | services-and-prices.pdf:1 | 0.616 | 0.616 |
| A09 | answerable | search | 0.551 | services-and-prices.pdf:1 payment-plans.md:2 | 0.559 | services-and-prices.pdf:1 payment-plans.md:3 | 0.551 | 0.559 |
| A10 | answerable | search | 0.561 | cancellation-policy.pdf:1 emergency-dental-care.pdf:2 | 0.613 | cancellation-policy.pdf:1 emergency-dental-care.pdf:3 | 0.561 | 0.613 |
| A11 | answerable | search | 0.527 | sedation-options.md:1 | 0.480 | sedation-options.md:1 | 0.527 | 0.480 |
| A12 | answerable | search | 0.513 | location-and-parking.md:1 | 0.702 | location-and-parking.md:1 | 0.513 | 0.702 |
| A13 | answerable | search | 0.720 | childrens-dentistry.md:1 | 0.707 | childrens-dentistry.md:1 | 0.720 | 0.707 |
| A14 | answerable | search | 0.686 | fillings-aftercare.md:1 | 0.655 | fillings-aftercare.md:1 | 0.686 | 0.655 |
| A15 | answerable | search | 0.460 | membership-plan.pdf:2 payment-plans.md:1 | 0.477 | membership-plan.pdf:2 payment-plans.md:1 | 0.460 | 0.477 |
| A16 | answerable | search | 0.657 | emergency-dental-care.pdf:1 opening-hours.md:2 | 0.676 | emergency-dental-care.pdf:1 opening-hours.md:3 | 0.657 | 0.676 |
| O01 | out_of_scope | search | 0.300 | - | 0.273 | - | - | - |
| O02 | out_of_scope | search | 0.569 | - | 0.462 | - | - | - |
| O03 | out_of_scope | search | 0.185 | - | 0.194 | - | - | - |
| O04 | out_of_scope | search | 0.444 | - | 0.420 | - | - | - |
| O05 | out_of_scope | search | 0.466 | - | 0.453 | - | - | - |
| O06 | out_of_scope | search | 0.171 | - | 0.130 | - | - | - |
| O07 | out_of_scope | search | 0.369 | - | 0.316 | - | - | - |
| O08 | out_of_scope | search | 0.485 | - | 0.511 | - | - | - |
| V01 | vague | clarify | 0.294 | - | 0.294 | - | - | - |
| V02 | vague | search | 0.278 | - | 0.412 | - | - | - |
| O09 | out_of_scope | clarify | 0.577 | - | 0.577 | - | - | - |
| O10 | out_of_scope | search | 0.504 | - | 0.509 | - | - | - |

Answerable questions whose expected sources are ALL in the top 5: original 16 of 16, rewrite 16 of 16.

RULE: switch SEARCH_ORIGINAL_WHEN_NO_HISTORY on only if the ORIGINAL wording keeps ALL expected sources of ALL practice answerables in the top 5.
RESULT: PASS. P1b MAY be switched on if the owner approves.

Rewrites that were clarifies are shown but a clarified question is never searched.
