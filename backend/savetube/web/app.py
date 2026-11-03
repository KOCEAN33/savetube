import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from savetube.config import Settings, get_settings
from savetube.db import make_engine, make_sessionmaker
from savetube.logs import emit
from savetube.web import api, auth

log = logging.getLogger(__name__)


async def _server_error(request: Request, exc: Exception) -> PlainTextResponse:
    # ServerErrorMiddleware re-raises after this, and uvicorn logs the traceback.
    # The path drops the query, which carries the OAuth code and state.
    emit(
        log,
        logging.ERROR,
        "web.request_failed",
        method=request.method,
        path=request.url.path,
        error=type(exc).__name__,
        agent=request.headers.get("user-agent"),
    )
    return PlainTextResponse("Internal Server Error", status_code=500)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.sqlalchemy_url)
        app.state.sessionmaker = make_sessionmaker(engine)
        yield
        await engine.dispose()

    app = FastAPI(title="savetube", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_exception_handler(Exception, _server_error)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    app.state.archive_root = settings.archive_root
    app.state.namespace = settings.namespace
    app.state.scheduler_url = settings.scheduler_url
    app.include_router(api.router)
    auth.install(app, settings)
    if settings.static_dir:
        _mount_spa(app, settings.static_dir)
    return app


def _mount_spa(app: FastAPI, root: Path) -> None:
    """Serve the Vite bundle, falling back to index.html for client-side routes."""
    root = root.resolve()
    index = root / "index.html"
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "auth/")):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)
