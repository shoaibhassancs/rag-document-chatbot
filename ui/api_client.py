"""HTTP client for the UI. Never raises to the page: every failure becomes an outcome whose `kind`
the view turns into a friendly message.

What is sent where:
  POST /chat          JSON {session_id, message}                      (no key)
  GET  /documents                                                      (no key)
  POST /ingest        multipart `files` and/or form `url`, X-API-Key   (key)
  DELETE /documents/{id}                       X-API-Key               (key)

The key comes from the APP_API_KEY environment variable. It is never shown, logged, stored in the
page state or included in any message; neither is raw error text from the server. The only server
text that reaches a user is the `answer` field of a /chat reply (the API's own refusal, clarifying
question or busy message) and the per-file `reason` codes of an ingest.
"""

import os
from dataclasses import dataclass, field
from uuid import UUID

import httpx

CHAT_STATUSES = {"answered", "refused", "clarify", "error"}
DEFAULT_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_TIMEOUT = (
    45.0  # the API can legitimately take a while: Groq retries and the Voyage wait cap
)


@dataclass
class ChatOutcome:
    kind: str  # "ok" | "unreachable" | "bad_input" | "unexpected"
    status: str | None = None  # answered | refused | clarify | error, when kind == "ok"
    answer: str = ""
    sources: list[dict] = field(default_factory=list)
    latency_ms: int | None = None


@dataclass
class DocsOutcome:
    kind: str  # "ok" | "unreachable" | "unexpected"
    documents: list[dict] = field(default_factory=list)


@dataclass
class IngestOutcome:
    # "ok" | "no_key" | "unauthorized" | "unavailable" | "too_many" | "bad_input" | "unreachable"
    # | "unexpected"
    kind: str
    results: list[dict] = field(default_factory=list)  # per file: name, status, reason_code, ...


@dataclass
class DeleteOutcome:
    # "ok" | "no_key" | "unauthorized" | "unavailable" | "not_found" | "conflict" | "bad_input"
    # | "unreachable" | "unexpected"
    kind: str


class ApiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self._key = api_key or ""
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def __repr__(self) -> str:  # never show the key
        return f"ApiClient(base_url={self._http.base_url!s}, key_set={bool(self._key)})"

    def close(self) -> None:
        self._http.close()

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    def _key_header(self) -> dict[str, str]:
        return {"X-API-Key": self._key}

    # ---- chat
    def chat(self, session_id: str, message: str) -> ChatOutcome:
        try:
            response = self._http.post("/chat", json={"session_id": session_id, "message": message})
        except httpx.TransportError:
            return ChatOutcome("unreachable")
        except httpx.HTTPError:
            return ChatOutcome("unexpected")
        if response.status_code == 422:
            return ChatOutcome("bad_input")
        if response.status_code not in (200, 503):
            return ChatOutcome("unexpected")
        try:
            body = response.json()
            status, answer = body["status"], body["answer"]
            sources = body.get("sources") or []
        except (ValueError, KeyError, TypeError):
            return ChatOutcome("unexpected")
        if (
            status not in CHAT_STATUSES
            or not isinstance(answer, str)
            or not isinstance(sources, list)
        ):
            return ChatOutcome("unexpected")
        latency = body.get("latency_ms")
        return ChatOutcome(
            "ok",
            status,
            answer,
            [s for s in sources if isinstance(s, dict)],
            latency if isinstance(latency, int) and not isinstance(latency, bool) else None,
        )

    # ---- documents
    def list_documents(self) -> DocsOutcome:
        try:
            response = self._http.get("/documents")
        except httpx.TransportError:
            return DocsOutcome("unreachable")
        except httpx.HTTPError:
            return DocsOutcome("unexpected")
        if response.status_code != 200:
            return DocsOutcome("unexpected")
        try:
            items = response.json()["documents"]
        except (ValueError, KeyError, TypeError):
            return DocsOutcome("unexpected")
        if not isinstance(items, list):
            return DocsOutcome("unexpected")
        return DocsOutcome("ok", [d for d in items if isinstance(d, dict)])

    def ingest(self, files: list[tuple[str, bytes]], url: str | None = None) -> IngestOutcome:
        if not self.has_key:
            return IngestOutcome("no_key")
        multipart = [("files", (name, data, "application/octet-stream")) for name, data in files]
        form = {"url": url} if url else None
        try:
            response = self._http.post(
                "/ingest", files=multipart or None, data=form, headers=self._key_header()
            )
        except httpx.TransportError:
            return IngestOutcome("unreachable")
        except httpx.HTTPError:
            return IngestOutcome("unexpected")
        code = response.status_code
        if code in (
            202,
            400,
        ):  # 400 = every item was rejected; the per-item reasons are in the body
            try:
                results = response.json()["results"]
            except (ValueError, KeyError, TypeError):
                return IngestOutcome("unexpected")
            if not isinstance(results, list):
                return IngestOutcome("unexpected")
            return IngestOutcome("ok", [r for r in results if isinstance(r, dict)])
        kinds = {401: "unauthorized", 503: "unavailable", 413: "too_many", 422: "bad_input"}
        return IngestOutcome(kinds.get(code, "unexpected"))

    def delete_document(self, document_id: str) -> DeleteOutcome:
        if not self.has_key:
            return DeleteOutcome("no_key")
        try:
            safe_id = str(UUID(document_id))
        except ValueError:
            return DeleteOutcome("bad_input")
        try:
            response = self._http.delete(f"/documents/{safe_id}", headers=self._key_header())
        except httpx.TransportError:
            return DeleteOutcome("unreachable")
        except httpx.HTTPError:
            return DeleteOutcome("unexpected")
        kinds = {
            204: "ok",
            401: "unauthorized",
            503: "unavailable",
            404: "not_found",
            409: "conflict",
            422: "bad_input",
        }
        return DeleteOutcome(kinds.get(response.status_code, "unexpected"))


def client_from_env() -> ApiClient:
    """The client the page uses. API_BASE_URL and APP_API_KEY come from the environment."""
    try:
        timeout = float(os.environ.get("API_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    return ApiClient(
        base_url=os.environ.get("API_BASE_URL") or DEFAULT_BASE_URL,
        api_key=os.environ.get("APP_API_KEY") or None,
        timeout=timeout,
    )
