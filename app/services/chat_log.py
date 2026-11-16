"""One row per /chat exchange in `chat_logs`, so evaluation and debugging can trace which chunks
produced each answer. Everything text-like is masked (emails, phone numbers) before it is written.
Never stored: API keys, prompts, the model's raw JSON. Writing is best-effort: the caller catches
failures so a database problem never changes the reply.
"""

from psycopg.types.json import Jsonb

from app.db import connect
from app.masking import mask_text, mask_value


class ChatLogger:
    def __init__(self, database_url: str):
        self._url = database_url

    def write(
        self,
        *,
        session_id: str | None,
        question: str,
        result,
        prompt_version: str | None,
        clarify_prompt_version: str | None,
    ) -> None:
        usage = {"answer": result.llm_usage, "clarify": result.clarify_usage}
        similarities = {str(r["chunk_id"]): r["similarity"] for r in result.retrieved}
        with connect(self._url) as conn:
            conn.execute(
                "INSERT INTO chat_logs (session_id, question, standalone_query, clarify_action, "
                "status, answer, retrieved_chunk_ids, used_chunk_ids, similarities, latency_ms, "
                "timings_ms, llm_usage, prompt_version, clarify_prompt_version, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    session_id or None,
                    mask_text(question),
                    mask_text(result.standalone_query) if result.standalone_query else None,
                    result.clarify_action,
                    result.status,
                    mask_text(result.answer),
                    [int(r["chunk_id"]) for r in result.retrieved],
                    [int(i) for i in result.used_chunk_ids],
                    Jsonb(mask_value(similarities)),
                    result.latency_ms,
                    Jsonb(mask_value(result.timings_ms)),
                    Jsonb(mask_value(usage)),
                    prompt_version,
                    clarify_prompt_version,
                    result.error,
                ],
            )
