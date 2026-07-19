"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings
from app.db import init_db
from app.routers import plot_checker
from app.templating import BASE_DIR, templates


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # In development the schema is created directly for convenience. Production
    # owns its schema through Alembic migrations and must not rely on this.
    if settings.app_env == "development":
        init_db()
    yield


app = FastAPI(title="EUDR Platform", version=__version__, lifespan=lifespan)

_static_dir = BASE_DIR / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(plot_checker.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"title": "EUDR Platform"},
    )
