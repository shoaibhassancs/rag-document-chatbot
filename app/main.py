import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import DbStatus, check_database, connect
from app.health import build_health
from app.ingest.embedder import Embedder, EmbeddingNotConfigured, build_embedder
from app.routes.documents import router as documents_router
from app.services.ingestion import embed_pending, fail_pending
from app.services.worker import IngestWorker
from app.startup import prepare_database

log = logging.getLogger(__name__)


def get_db_checker() -> Callable[[str], DbStatus]:
    return check_database


def create_app(embedder_factory: Callable[[Settings], Embedder] = build_embedder) -> FastAPI:
    """Build the app. `embedder_factory` is replaced by a fake in tests."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Honour dependency overrides so tests can point the whole app at a test database.
        settings = app.dependency_overrides.get(get_settings, get_settings)()
        logging.basicConfig(level=settings.log_level)
        prepare_database(settings)  # migrations, then fail on an embedding config mismatch
        app.state.embedder = None

        def run_embedding() -> None:
            if app.state.embedder is None:
                try:
                    app.state.embedder = embedder_factory(settings)
                except EmbeddingNotConfigured as exc:
                    fail_pending(settings.database_url, str(exc))
                    return
            embed_pending(settings.database_url, app.state.embedder)

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

    @app.get("/health")
    def health(
        settings: Settings = Depends(get_settings),
        check_db: Callable[[str], DbStatus] = Depends(get_db_checker),
    ) -> JSONResponse:
        report, code = build_health(settings, check_db)
        return JSONResponse(report, status_code=code)

    return app


app = create_app()
