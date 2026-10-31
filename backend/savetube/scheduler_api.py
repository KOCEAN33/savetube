"""The scheduler's internal HTTP API (docs/plan/runtime.md, Scheduler API): a
user action the web asks the scheduler to do now. Cluster-internal, with no
auth: only the web calls it, through a ClusterIP Service."""

import asyncio

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube import encode, watch
from savetube.config import Settings
from savetube.db.models import Source
from savetube.integrity import Tools
from savetube.poll import disabled

# An in-flight probe gets this long after SIGTERM before the server closes.
_GRACEFUL_SECONDS = 5


class Requests:
    """The user's asks the API hands to the scheduler's loops, each with the
    event that wakes its loop. In memory only: a restart drops them, and the
    source then polls at its interval, the staged videos move by move_reason's
    other rules."""

    def __init__(self) -> None:
        self._polls: list[int] = []
        self.poll_wake = asyncio.Event()
        self._move = False
        self.move_wake = asyncio.Event()

    def ask_poll(self, source_id: int) -> None:
        if source_id not in self._polls:
            self._polls.append(source_id)
        self.poll_wake.set()

    def take_poll(self) -> int | None:
        """The oldest requested source, None when none waits."""
        return self._polls.pop(0) if self._polls else None

    def ask_move(self) -> None:
        self._move = True
        self.move_wake.set()

    def take_move(self) -> bool:
        asked, self._move = self._move, False
        return asked


def create_app(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    *,
    probes: asyncio.Lock,
    probe: watch.Probe,
    tools: Tools,
    requests: Requests,
    encodes: asyncio.Lock,
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

    @app.post("/poll/{source_id}", status_code=202)
    async def poll_now(source_id: int) -> Response:
        """Poll the source next, without waiting for the poll: a first listing
        of a big channel takes minutes."""
        async with sessionmaker() as session:
            source = await session.get(Source, source_id)
            if source is None:
                raise HTTPException(status_code=404, detail="no such source")
            if await disabled(session, source):
                raise HTTPException(status_code=409, detail="the source is disabled")
        requests.ask_poll(source_id)
        return Response(status_code=202)

    @app.post("/staging/move", status_code=202)
    async def move_staged() -> Response:
        """Move every staged video now, without waiting for the move: a batch is
        up to 80 GB."""
        if settings.staging_root is None:
            raise HTTPException(status_code=409, detail="no staging volume")
        requests.ask_move()
        return Response(status_code=202)

    @app.post("/encode/{job_id}/cancel", status_code=204)
    async def cancel_encode(job_id: int) -> Response:
        """Delete a queued encode job's file, unless the encoder claimed it."""
        try:
            await encode.cancel(sessionmaker, settings, job_id, lock=encodes)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such encode job") from None
        except encode.NotCancellable as e:
            raise HTTPException(status_code=409, detail=f"the job is {e}") from None
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
