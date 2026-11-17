# Case study outline (draft; every result is TBD)

**Status: outline written Nov 17, 2026, before the evaluation has run. No number appears here until the real runs exist; results sections say TBD.**

## The problem
- A small service business (the demo: a fictional dental clinic) answers the same questions by phone, email and chat all day.
- Staff want customers and employees to ask in plain English and get answers from the clinic's own documents, with a source, not a guess.
- The bot must say "I don't know" when the documents do not cover a question, and ask when a question is too vague to answer.

## Constraints
- Budget $0: free tiers only (Groq for the models, Voyage for embeddings), no paid API.
- Free-tier limits shape the design: Voyage 3 requests a minute, each Groq model with its own per-minute and per-day token limits, so about two questions a minute.
- Shown as a video and screenshots, not a live link; everything runs locally in Docker. All data is fictional.

## Design
- Ingestion: PDF, Markdown, text and one web page, cleaned, chunked with file, page and section kept, embedded with Voyage, stored in Postgres with pgvector; unchanged files are skipped by content hash.
- Query: one small model call turns a follow-up into a standalone query or asks one clarifying question; the query is embedded, the top 5 chunks are retrieved, weak matches are dropped, and a grounded prompt answers from what is left.
- Citations are built in code from chunk metadata, never from model text; every exchange is logged with emails and phone numbers masked.
- FastAPI service layer (reusable by later projects over HTTP), Streamlit page for the demo.

## The decisions that mattered
- **The refusal gate:** a similarity threshold (0.40) refuses clearly unrelated questions without calling the answer model; the answer prompt handles the near misses. It was calibrated on a small practice set, then kept apart from the evaluation questions. A real finding to report: a correct question can be refused when its rewritten search wording scores just under the gate.
- **The clarify step and what we measured:** one call decides search or clarify. Practice checks showed the first prompt asking when it should search; a second prompt fixed that but over-asked on insurance and payment questions; the final setup (the 120b model with `clarify_v2`) met the bars on clear and vague questions and missed the bar on follow-ups (7 of 12 right, practice checks, not the evaluation). We reported that as a known limitation instead of tuning against the exam.
- **Honest evaluation:** 40 questions approved by the owner (29 answerable, 7 out of scope, 4 vague); practice sets kept separate; a held-out set used once; no question or prompt edited after seeing results.
- **Judge fixes:** the judge model is from a different family than the answerer; two bugs found in a 3-question smoke test were fixed before the real run (the clinic's own name counted as an unsupported claim, and faithfulness was checked against only the chunks the model cited instead of every chunk it was given). The owner hand-checks about 10 verdicts.

## Results
- TBD: from the real evaluation (retrieval hit rate, faithfulness, answer correctness, refusal accuracy, status accuracy, latency; both answer models compared, final model chosen from the numbers).
- TBD: before and after table if a change is made after the first run.

## Limitations
- Free-tier throughput; a "busy" reply is possible at peak.
- The clarify step sometimes asks a narrower question on a follow-up.
- Answers can vary between runs; the practice-set tuning is not proof, the evaluation is.
- Scanned PDFs, images and tables inside PDFs are not supported; `/chat` and `/documents` are open (local showcase).

## What I would do next
- Re-run the evaluation after any change and keep the before and after table.
- Try a better follow-up handling and a gate that uses more than one score, checked on practice data first, then on the evaluation.
- For a real client: swap providers by config (paid models, data isolation), add authentication on every route, a managed database and monitoring.
