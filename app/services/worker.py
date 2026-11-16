"""A single background thread that runs the embedding job. One thread means one run at a time,
so the Voyage limiter is respected even when several uploads arrive together: each wake-up
embeds everything that is pending, in shared batches."""

import logging
import threading
from collections.abc import Callable

from app.ingest.embedder import EmbeddingError

log = logging.getLogger(__name__)


class IngestWorker:
    def __init__(self, run: Callable[[], object]):
        self._run = run
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="ingest-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop = True
        self._wake.set()
        self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stop:
                return
            try:
                self._run()
            except Exception as exc:  # keep the thread alive; the documents are marked failed
                detail = f": {exc}" if isinstance(exc, EmbeddingError) else ""
                log.error("Embedding run failed (%s%s)", type(exc).__name__, detail)
