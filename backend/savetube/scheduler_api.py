"""The scheduler's internal HTTP API (docs/plan/runtime.md, Scheduler API): a
user action the web asks the scheduler to do now. Cluster-internal, with no
auth: only the web calls it, through a ClusterIP Service."""

import asyncio

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube import watch
from savetube.config import Settings
from savetube.integrity import Tools

# An in-flight probe gets this long after SIGTERM before the server closes.
_GRACEFUL_SECONDS = 5


def create_app(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    *,
    probes: asyncio.Lock,
    probe: watch.Probe,
    tools: Tools,
) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/probe/{video_id}", status_code=204)
    async def probe_formats(video_id: str) -> Response:
        """Probe the video's formats now and store them in its format_watch row."""
        try:
            await watch.probe_now(
                sessionmaker, settings, video_id, probe=probe, tools=tools, lock=probes
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="no such video") from None
        except watch.NotComplete:
            raise HTTPException(status_code=409, detail="the video is not complete") from None
        return Response(status_code=204)

    return app


async def serve(app: FastAPI, port: int) -> None:
    """Serve `app` in the caller's asyncio loop until SIGTERM or SIGINT. uvicorn
    catches the signal, closes the server, then raises it again, so the
    process stops as it would without the server."""
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            lifespan="off",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=_GRACEFUL_SECONDS,
        )
    )
    await server.serve()
