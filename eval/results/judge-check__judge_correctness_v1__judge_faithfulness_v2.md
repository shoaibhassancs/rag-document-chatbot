# Judge check: judge-check__judge_correctness_v1__judge_faithfulness_v2

Hand-written controls from the sample documents, NOT the evaluation set. Not a result.

- Run: 2026-11-18 06:28 UTC
- Judge model: qwen/qwen3.8-27b; prompts: judge_correctness_v1__judge_faithfulness_v2

| id | tests | check | expected | judge | result | reason |
|---|---|---|---|---|---|---|
| J01 | good: terse bare figure | correct | 1 | 1 | PASS | The answer correctly states 2 working days, matching the expected answer. |
| J01 | good: terse bare figure | faithful | 1 | 1 | PASS | The excerpt states that a complaint is acknowledged within 2 working days, which matches the answer. |
| J02 | good: harmless extra true detail | correct | 1 | 1 | PASS | The answer correctly states the 5 to 10 minute timeframe after mask removal, with an extra true detail about breathing pure oxygen. |
| J02 | good: harmless extra true detail | faithful | 1 | 1 | PASS | The answer directly matches the text in chunk 1 word for word. |
| J03 | good: two facts, paraphrased | correct | 1 | 1 | PASS | The answer correctly identifies Dr. Maya Patel as the principal dentist who reviews the complaint and replies within 10 working days, matching all facts in the  |
| J03 | good: two facts, paraphrased | faithful | 1 | 1 | PASS | All claims (Dr. Maya Patel, principal dentist, can review, reply within 10 working days) are directly stated in chunk 1. |
| J04 | unfaithful: wrong number | correct | 0 | 0 | PASS | The answer states 30 working days instead of the correct 15 working days. |
| J04 | unfaithful: wrong number | faithful | 0 | 0 | PASS | The answer states 30 working days, but the excerpt says 15 working days. |
| J05 | unfaithful: invented extra fact | correct | either | error (LLMRequestError) | ERROR |  |
| J05 | unfaithful: invented extra fact | faithful | 0 | 0 | PASS | The claim that every complaint is reviewed by an external ombudsman each quarter is not supported by the provided excerpts. |
| J06 | unfaithful: true fact that is only in a chunk NOT supplied | correct | 1 | 1 | PASS | The answer correctly identifies the office manager or the principal dentist as the only ones who can decide on a refund, matching the expected answer. |
| J06 | unfaithful: true fact that is only in a chunk NOT supplied | faithful | 0 | 0 | PASS | The excerpts contain no information about who can decide on a refund or mention an office manager or principal dentist. |
| J07 | wrong: wrong figures | correct | 0 | 0 | PASS | The answer states $500–$3,000 instead of the correct $300–$1,500 range. |
| J07 | wrong: wrong figures | faithful | not scored | 0 | REPORT | The answer states the range is $500–$3,000, but the excerpt says $300–$1,500, which is a direct contradiction. |
| J08 | wrong: opposite yes/no | correct | 0 | 0 | PASS | The answer says nitrous oxide is not suitable for children, directly contradicting the expected answer which states it is suitable for children and adults. |
| J08 | wrong: opposite yes/no | faithful | not scored | 0 | REPORT | The answer states it is not suitable for children, which directly contradicts the excerpt that says it is suitable for children and adults. |
| J09 | wrong: wrong person | correct | 0 | 0 | PASS | The answer names Dr. Maya Patel instead of the correct office manager, Grace Whitfield. |
| J09 | wrong: wrong person | faithful | not scored | 0 | REPORT | The excerpt identifies Grace Whitfield as the office manager, not Dr. Maya Patel. |
| J10 | strictness: core fact right, side detail omitted (reported, not scored) | correct | either | 0 | REPORT | The answer omits key details: automatic card-on-file payments and the no interest or fees condition. |
| J10 | strictness: core fact right, side detail omitted (reported, not scored) | faithful | either | 1 | REPORT | The answer accurately reflects the 25% first-day payment and the balance split into 3 equal monthly payments as stated in the excerpt. |
| J11 | refusal text: no credit for correctness | correct | 0 | 0 | PASS | The answer states it cannot find the information instead of providing the expected '2 working days'. |
| J11 | refusal text: no credit for correctness | faithful | not scored | 1 | REPORT | The answer makes no factual claims to verify; it simply states the information was not found and directs the user to contact the clinic. |
| J12 | empty-like answer: no credit for correctness | correct | 0 | 0 | PASS | The answer states it does not know, failing to provide the expected information about nitrous oxide working within a few minutes. |
| J12 | empty-like answer: no credit for correctness | faithful | not scored | 1 | REPORT | The answer makes no factual claims to verify against the excerpts. |

**Negatives caught 9 of 9; positives kept 7 of 7; errored calls 1; strictness item (correct, faithful): (0, 1). No miss.**
