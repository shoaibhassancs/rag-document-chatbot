"""Document routes: POST /ingest, GET /documents, DELETE /documents/{id}. Thin: parsing and
storage live in app/ingest and app/services."""

from pathlib import PurePosixPath
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from app.auth import require_api_key
from app.config import Settings, get_settings
from app.db import connect
from app.ingest.loaders import LoadedDocument, LoadError, load_bytes, load_url
from app.services.documents import delete_document, list_documents
from app.services.ingestion import ItemResult, register_document

router = APIRouter()


def _register(conn: psycopg.Connection, doc: LoadedDocument, settings: Settings) -> ItemResult:
    try:
        result = register_document(
            conn,
            doc,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
            embedding_model=settings.embedding_model,
        )
        conn.commit()
        return result
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        return ItemResult(
            doc.name,
            "rejected",
            reason_code="conflict",
            detail="Another upload of this name is in progress.",
        )


@router.post("/ingest", status_code=202, dependencies=[Depends(require_api_key)])
def ingest(
    request: Request,
    files: list[UploadFile] = File(default=None),
    url: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Accept files and/or one URL. Parsing and chunking happen now (so bad files are rejected
    immediately); embedding runs in the background, because free-tier throttling takes minutes."""
    files = files or []
    url = (url or "").strip() or None
    if not files and not url:
        raise HTTPException(422, "Send at least one file (field 'files') or a 'url'.")
    if len(files) > settings.max_files_per_request:
        raise HTTPException(413, f"At most {settings.max_files_per_request} files per request.")
    if (
        settings.embedding_provider != "voyage"
        or settings.voyage_api_key is None
        or not settings.voyage_api_key.get_secret_value()
    ):
        raise HTTPException(503, "The embedding key (VOYAGE_API_KEY) is not configured.")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    results: list[ItemResult] = []
    with connect(settings.database_url) as conn:
        for upload in files:
            name = PurePosixPath((upload.filename or "").replace("\\", "/")).name
            if not name:
                results.append(ItemResult("(no name)", "rejected", reason_code="bad_name"))
                continue
            data = upload.file.read(max_bytes + 1)
            if len(data) > max_bytes:
                results.append(
                    ItemResult(name, "rejected", reason_code="too_large",
                               detail=f"Larger than {settings.max_upload_mb} MB.")
                )  # fmt: skip
                continue
            try:
                doc = load_bytes(name, data)
            except LoadError as exc:
                results.append(
                    ItemResult(name, "rejected", reason_code=exc.code, detail=exc.message)
                )
                continue
            results.append(_register(conn, doc, settings))
        if url:
            try:
                doc = load_url(
                    url,
                    allow_private=settings.allow_private_urls,
                    timeout=settings.url_timeout_seconds,
                    max_bytes=settings.url_max_bytes,
                )
            except LoadError as exc:
                results.append(
                    ItemResult(url, "rejected", reason_code=exc.code, detail=exc.message)
                )
            else:
                results.append(_register(conn, doc, settings))

    if any(r.status == "queued" for r in results):
        request.app.state.worker.notify()
    all_rejected = all(r.status == "rejected" for r in results)
    return JSONResponse(
        {"results": [r.as_dict() for r in results]}, status_code=400 if all_rejected else 202
    )


@router.get("/documents")
def documents(settings: Settings = Depends(get_settings)) -> dict:
    with connect(settings.database_url) as conn:
        items = list_documents(conn)
    return {"documents": items, "total": len(items)}


@router.delete("/documents/{document_id}", dependencies=[Depends(require_api_key)])
def delete(document_id: UUID, settings: Settings = Depends(get_settings)) -> Response:
    with connect(settings.database_url) as conn:
        outcome = delete_document(conn, document_id)
        conn.commit()
    if outcome == "not_found":
        raise HTTPException(404, "No such document.")
    if outcome == "ingesting":
        raise HTTPException(
            409, "The document is still being embedded. Try again when it is ready or failed."
        )
    return Response(status_code=204)
