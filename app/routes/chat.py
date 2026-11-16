"""POST /chat. Thin: validation and status codes only; the pipeline is in app/services/chat.py.

Open endpoint by design (local showcase; see SPEC). `status: error` is returned with HTTP 503 so
callers (projects 3 and 6) can tell a failure from a normal reply without parsing the text.
"""

import logging
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.config import Settings, get_settings
from app.generation.llm import LLMConfigError
from app.ingest.embedder import EmbeddingNotConfigured
from app.services.chat import ChatService, error_result

log = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()


class Source(BaseModel):
    file: str
    page: int | None
    section: str | None
    document_id: str


class ChatResponse(BaseModel):
    answer: str
    status: Literal["answered", "refused", "clarify", "error"]
    sources: list[Source]
    latency_ms: int


def get_chat_service(request: Request, settings: Settings = Depends(get_settings)) -> ChatService:
    provider: Callable[[Settings], ChatService] = request.app.state.chat_service_provider
    return provider(settings)


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    try:
        service = get_chat_service(request, settings)
    except (LLMConfigError, EmbeddingNotConfigured) as exc:
        log.error("chat_unavailable %s: %s", type(exc).__name__, exc)
        result = error_result(settings, type(exc).__name__)
    else:
        result = service.chat(body.session_id, body.message)
    payload = ChatResponse(
        answer=result.answer,
        status=result.status,
        sources=[Source(**s) for s in result.sources],
        latency_ms=result.latency_ms,
    )
    code = 503 if result.status == "error" else 200
    return JSONResponse(payload.model_dump(), status_code=code)
