"""One download job, archive or recording, from the `job` row to the tree."""

import asyncio
import json
import logging
import multiprocessing
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Text, column, func, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import Job, JobKind, JobState, Video, VideoState
from savetube.limits import admit, load, lock, next_window_start, running
from savetube.pipeline import from_shorts_tab, prepare_incoming, settings_channel, store
from savetube.ytdl import DEFAULT_FORMAT, build_params, copy_cookies, download, video_url

log = logging.getLogger(__name__)

LOG_TAIL_CHARS = 4000

Runner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# Puts the `job` row on the queue and returns the queue's job id.
Enqueue = Callable[[int], Awaitable[int]]
# Queue jobs left running by a dead worker, as (queue job id, `job` id).
Stalled = Callable[[], Awaitable[list[tuple[int, int]]]]
# Ends a queue job as failed, so no worker takes it again.
Finish = Callable[[int], Awaitable[None]]

STALLED_LOG = "worker stopped during the download"


class DownloadFailed(Exception):
    pass


def _child(url: str, params: dict[str, Any], out: str) -> None:
    try:
        result = {"info": download(url, params)}
    except BaseException as e:  # noqa: BLE001 - reported to the parent, which decides
        result = {"error": f"{type(e).__name__}: {e}"}
    Path(out).write_text(json.dumps(result))
    if "error" in result:
        raise SystemExit(1)


async def in_child(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one download in its own process.

    A Python thread cannot be stopped from outside, and a cancel or a hung
    download needs a process kill.
    """
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "result.json"
        proc = ctx.Process(target=_child, args=(url, params, str(out)), daemon=True)
        proc.start()
        try:
            await asyncio.to_thread(proc.join)
        except asyncio.CancelledError:
            proc.kill()
            await asyncio.to_thread(proc.join)
            raise
        result = json.loads(out.read_text()) if out.exists() else {}
        if proc.exitcode != 0 or "info" not in result:
            raise DownloadFailed(result.get("error") or f"exit code {proc.exitcode}")
        return result["info"]


async def in_process(url: str, params: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(download, url, params)


async def _set_video_state(session: AsyncSession, video_id: str, state: VideoState) -> None:
    video = await session.get(Video, video_id)
    # A video stored by an earlier job stays complete through a re-download.
    if video is not None and video.state is not VideoState.COMPLETE:
        video.state = state


async def reclaim_stalled(
    sessionmaker: async_sessionmaker, settings: Settings, *, stalled: Stalled, finish: Finish
) -> None:
    """Fail the jobs of a dead worker. Their rows would stay running and hold
    download slots forever. The next poll queues each video again."""
    for queue_job_id, job_id in await stalled():
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            if job is not None and job.state in (JobState.QUEUED, JobState.RUNNING):
                log.warning("job %s: %s", job_id, STALLED_LOG)
                job.state = JobState.FAILED
                job.finished_at = datetime.now(UTC)
                job.log_tail = STALLED_LOG
                if job.video_id is not None:
                    await _set_video_state(session, job.video_id, VideoState.FAILED)
                await session.commit()
        shutil.rmtree(settings.incoming_root / str(job_id), ignore_errors=True)
        await finish(queue_job_id)


_queue_jobs = table("procrastinate_jobs", column("id"), column("status"))
# Queue jobs that still lead to a run of their `job` row.
_LIVE_QUEUE_JOBS = select(_queue_jobs.c.id).where(
    _queue_jobs.c.status.cast(Text).in_(["todo", "doing"])
)


async def dispatch(session: AsyncSession, now: datetime, enqueue: Enqueue) -> list[int]:
    """Put the oldest waiting archive jobs on the queue, one per free slot, and
    only inside the download window. Return their ids. Commits.

    A waiting job is a queued archive job with no queue job. A dispatched job
    holds its slot until a worker takes it or hands it back.
    """
    await lock(session)
    limits = await load(session)
    queued = (Job.kind == JobKind.ARCHIVE) & (Job.state == JobState.QUEUED)
    # A queue job that ended before the worker marked the row, such as an
    # error or a stopped worker, would hold its slot for good.
    await session.execute(
        update(Job)
        .where(queued, Job.queue_job_id.is_not(None), Job.queue_job_id.not_in(_LIVE_QUEUE_JOBS))
        .values(queue_job_id=None)
    )
    opens = next_window_start(now, limits.window_start, limits.window_end)
    await session.execute(
        update(Job)
        .where(queued, Job.queue_job_id.is_(None), Job.deferred_until.is_distinct_from(opens))
        .values(deferred_until=opens)
    )
    started: list[int] = []
    if opens is None:
        dispatched = await session.scalar(
            select(func.count()).select_from(Job).where(queued, Job.queue_job_id.is_not(None))
        )
        free = limits.max_concurrent - await running(session) - dispatched
        waiting = await session.scalars(
            select(Job.id)
            .where(queued, Job.queue_job_id.is_(None))
            .order_by(Job.id)
            .limit(max(free, 0))
        )
        for job_id in list(waiting):
            try:
                queue_job_id = await enqueue(job_id)
            except Exception:
                log.exception("enqueue of job %s failed", job_id)
                break
            await session.execute(
                update(Job).where(Job.id == job_id).values(queue_job_id=queue_job_id)
            )
            started.append(job_id)
    await session.commit()
    return started


async def run(
    job_id: int,
    settings: Settings,
    *,
    live: bool,
    runner: Runner,
    sessionmaker: async_sessionmaker | None = None,
) -> None:
    """Run one job. An archive job outside the download window, or with no free
    slot, goes back to waiting for the dispatcher. A recording ignores the limits."""
    sessionmaker = sessionmaker or default_sessionmaker()
    ratelimit = None
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None or job.video_id is None:
            raise LookupError(f"job {job_id} has no video")
        if not live:
            limits = await admit(session, datetime.now(UTC))
            if limits is None:
                # A core UPDATE: the row loaded above may predate the
                # dispatcher's commit of queue_job_id.
                await session.execute(update(Job).where(Job.id == job_id).values(queue_job_id=None))
                await session.commit()
                return
            ratelimit = limits.rate_limit_bytes
        video_id = job.video_id
        channel = await settings_channel(session, video_id)
        use_cookies = channel is not None and channel.use_cookies
        fmt = channel.format if channel is not None else DEFAULT_FORMAT
        shorts = await from_shorts_tab(session, video_id)
        job.deferred_until = None
        job.state = JobState.RUNNING
        job.attempts += 1
        job.started_at = datetime.now(UTC)
        await _set_video_state(session, video_id, VideoState.DOWNLOADING)
        await session.commit()

    # Any failure past RUNNING must reach the row, or the job stays running and
    # no poll queues the video again.
    try:
        incoming = prepare_incoming(settings.incoming_root, job_id)
        # Outside `incoming`, so the cookie jar never moves into the tree.
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            cookiefile = None
            if use_cookies:
                if settings.cookies_file is None:
                    raise RuntimeError(
                        "the channel uses cookies and SAVETUBE_COOKIES_FILE is unset"
                    )
                cookiefile = copy_cookies(settings.cookies_file, Path(private))
            params = build_params(
                incoming,
                format=fmt,
                cookiefile=cookiefile,
                live=live,
                ratelimit=ratelimit,
            )
            info = await runner(video_url(video_id), params)

        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            await store(
                session,
                job=job,
                info=info,
                incoming=incoming,
                archive_root=settings.archive_root,
                from_shorts_tab=shorts,
            )
            await session.commit()
    except Exception as e:
        # A retry is a new job with a new directory, so a partial download
        # would otherwise stay on the dataset for good.
        shutil.rmtree(settings.incoming_root / str(job_id), ignore_errors=True)
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            job.state = JobState.FAILED
            job.finished_at = datetime.now(UTC)
            job.log_tail = (str(e) or type(e).__name__)[-LOG_TAIL_CHARS:]
            await _set_video_state(session, video_id, VideoState.FAILED)
            await session.commit()
        raise
