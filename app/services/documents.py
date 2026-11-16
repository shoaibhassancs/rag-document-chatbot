"""List and delete documents. Plain functions over a psycopg connection."""

from uuid import UUID

import psycopg

_LIST_SQL = """
SELECT d.id, d.name, d.source_type, d.status, d.error, d.page_count, d.chunk_total,
       d.content_hash, d.created_at, d.updated_at,
       count(c.id) FILTER (WHERE c.embedding IS NOT NULL) AS chunks_done,
       max(c.embedding_model) AS embedding_model
FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
GROUP BY d.id
ORDER BY d.name
"""


def list_documents(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(_LIST_SQL).fetchall()
    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "source_type": r[2],
            "status": r[3],
            "error": r[4],
            "page_count": r[5],
            "chunks": r[6],
            "content_hash": r[7][:12],
            "created_at": r[8].isoformat(),
            "updated_at": r[9].isoformat(),
            "chunks_done": r[10],
            "embedding_model": r[11],
        }
        for r in rows
    ]


def delete_document(conn: psycopg.Connection, document_id: UUID) -> str:
    """Returns 'deleted', 'not_found' or 'ingesting' (a document being embedded is not deleted)."""
    row = conn.execute("SELECT status FROM documents WHERE id = %s", [document_id]).fetchone()
    if row is None:
        return "not_found"
    if row[0] == "ingesting":
        return "ingesting"
    conn.execute("DELETE FROM documents WHERE id = %s", [document_id])  # chunks cascade
    return "deleted"
