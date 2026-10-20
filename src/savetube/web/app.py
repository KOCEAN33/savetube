from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from savetube.config import Settings, get_settings
from savetube.db import make_engine, make_sessionmaker
from savetube.web import api, auth


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.sqlalchemy_url)
        app.state.sessionmaker = make_sessionmaker(engine)
        yield
        await engine.dispose()

    app = FastAPI(title="savetube", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

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
