from collections.abc import Callable

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.db import DbStatus, check_database
from app.health import build_health

app = FastAPI(title="RAG Document Chatbot")


def get_db_checker() -> Callable[[str], DbStatus]:
    return check_database


@app.get("/health")
def health(
    settings: Settings = Depends(get_settings),
    check_db: Callable[[str], DbStatus] = Depends(get_db_checker),
) -> JSONResponse:
    report, code = build_health(settings, check_db)
    return JSONResponse(report, status_code=code)
