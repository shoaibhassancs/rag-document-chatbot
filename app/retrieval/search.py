"""Vector search over the stored chunks, and the similarity threshold.

Similarity is cosine similarity = 1 - cosine distance, computed by pgvector (`<=>`), so it matches
the HNSW index (vector_cosine_ops). Only chunks embedded with the configured model are searched,
so vectors from different models are never compared. With ~100 chunks the HNSW index returns
exact results; on a much larger corpus the index is approximate (ef_search), which is fine.
"""

from dataclasses import dataclass

import psycopg

from app.services.ingestion import vector_literal

_SQL = """
SELECT c.id, c.document_id, d.name, c.page, c.section, c.text,
       1 - (c.embedding <=> %(q)s::vector) AS similarity
FROM chunks c JOIN documents d ON d.id = c.document_id
WHERE c.embedding IS NOT NULL AND c.embedding_model = %(model)s
ORDER BY c.embedding <=> %(q)s::vector
LIMIT %(k)s
"""


@dataclass
class Hit:
    chunk_id: int
    document_id: str
    file: str
    page: int | None
    section: str | None
    text: str
    similarity: float


def search(
    conn: psycopg.Connection, query_vector: list[float], *, top_k: int, embedding_model: str
) -> list[Hit]:
    rows = conn.execute(
        _SQL, {"q": vector_literal(query_vector), "model": embedding_model, "k": top_k}
    ).fetchall()
    return [Hit(r[0], str(r[1]), r[2], r[3], r[4], r[5], float(r[6])) for r in rows]


def apply_threshold(hits: list[Hit], threshold: float | None) -> list[Hit]:
    """Drop weak matches. `None` (not configured) keeps everything."""
    if threshold is None:
        return hits
    return [h for h in hits if h.similarity >= threshold]
