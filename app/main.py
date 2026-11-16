import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import DbStatus, check_database, connect
from app.generation.answer import Answerer, build_answerer
from app.health import build_health
from app.ingest.embedder import (
    CachingEmbedder,
    Embedder,
    EmbeddingNotConfigured,
    build_embedder,
)
from app.masking import install_log_masking
from app.memory import MemoryStore
from app.routes.chat import router as chat_router
from app.routes.documents import router as documents_router
from app.services.chat import ChatService, QueryPreparer
from app.services.chat_log import ChatLogger
from app.services.clarify import build_preparer
from app.services.ingestion import embed_pending, fail_pending
from app.services.worker import IngestWorker
from app.startup import prepare_database

log = logging.getLogger(__name__)


def get_db_checker() -> Callable[[str], DbStatus]:
    return check_database


def create_app(
    embedder_factory: Callable[[Settings], Embedder] = build_embedder,
    answerer_factory: Callable[[Settings], Answerer] = build_answerer,
    preparer_factory: Callable[[Settings, MemoryStore], QueryPreparer | None] = build_preparer,
) -> FastAPI:
    """Build the app. The three factories are replaced by fakes in tests."""
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Honour dependency overrides so tests can point the whole app at a test database.
        settings = app.dependency_overrides.get(get_settings, get_settings)()
        logging.basicConfig(level=settings.log_level)
        install_log_masking()  # emails and phone numbers never reach the log output
        prepare_database(settings)  # migrations, then fail on an embedding config mismatch
        app.state.embedder = None
        app.state.chat_service = None

        def get_embedder(current: Settings) -> CachingEmbedder:
            """One embedder (and so one rate limiter) shared by ingestion and chat."""
            with lock:
                if app.state.embedder is None:
                    app.state.embedder = CachingEmbedder(embedder_factory(current))
                return app.state.embedder

        def chat_service_provider(current: Settings) -> ChatService:
            with lock:
                service = app.state.chat_service
            if service is None:
                embedder = get_embedder(current)
                memory = MemoryStore(
                    current.database_url, current.history_turns, current.session_ttl_hours
                )
                service = ChatService(
                    current,
                    embedder,
                    answerer_factory(current),
                    preparer_factory(current, memory),
                    memory=memory,
                    chat_logger=ChatLogger(current.database_url),
                )
                with lock:
                    app.state.chat_service = service
            return service

        app.state.chat_service_provider = chat_service_provider

        def run_embedding() -> None:
            try:
                embedder = get_embedder(settings)
            except EmbeddingNotConfigured as exc:
                fail_pending(settings.database_url, str(exc))
                return
            embed_pending(settings.database_url, embedder)

        worker = IngestWorker(run_embedding)
        worker.start()
        app.state.worker = worker
        with connect(settings.database_url) as conn:
            interrupted = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM documents WHERE status = 'ingesting')"
            ).fetchone()[0]
        if interrupted:  # resume work that a restart interrupted
            worker.notify()
        yield
        worker.stop()

    app = FastAPI(title="RAG Document Chatbot", lifespan=lifespan)
    app.include_router(documents_router)
    app.include_router(chat_router)

    @app.get("/health")
    def health(
        settings: Settings = Depends(get_settings),
        check_db: Callable[[str], DbStatus] = Depends(get_db_checker),
    ) -> JSONResponse:
        report, code = build_health(settings, check_db)
        return JSONResponse(report, status_code=code)

    return app


app = create_app()
