"""One download job, archive or recording, from the `job` row to the tree."""

import asyncio
import contextlib
import json
import logging
import multiprocessing
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from sqlalchemy import Text, column, func, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube import logs
from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import Channel, Job, JobKind, JobState, Video, VideoState
from savetube.limits import admit, load, lock, next_window_start, running
from savetube.logs import emit, human_bytes, human_duration
from savetube.pipeline import from_shorts_tab, prepare_incoming, settings_channel, store
from savetube.poll import MAX_ARCHIVE_ATTEMPTS
from savetube.ytdl import DEFAULT_FORMAT, build_params, copy_cookies, download, video_url

log = logging.getLogger(__name__)

LOG_TAIL_CHARS = 4000
TITLE_CHARS = 80
# The child sends progress at most this often, and the parent writes the job
# row at most every PROGRESS_WRITE_SECONDS.
PROGRESS_SEND_SECONDS = 1
PROGRESS_WRITE_SECONDS = 5

# Stores a running download's progress (savetube.logs.progress) on its job row.
Report = Callable[[dict[str, Any]], Awaitable[None]]
Runner = Callable[[str, dict[str, Any], Report], Awaitable[dict[str, Any]]]
# Puts the `job` row on the queue and returns the queue's job id.
Enqueue = Callable[[int], Awaitable[int]]
# Queue jobs left running by a dead worker, as (queue job id, `job` id).
Stalled = Callable[[], Awaitable[list[tuple[int, int]]]]
# Ends a queue job as failed, so no worker takes it again.
Finish = Callable[[int], Awaitable[None]]

STALLED_LOG = "worker stopped during the download"


class DownloadFailed(Exception):
    pass


def _log_format(info: dict[str, Any]) -> None:
    size = info.get("filesize") or info.get("filesize_approx")
    emit(
        log,
        logging.INFO,
        "download.format",
        format=info.get("format_id"),
        res=info.get("resolution"),
        size_est=human_bytes(size) if size else None,
        duration=human_duration(info["duration"]) if info.get("duration") else None,
    )


def _on_postprocessor(d: dict[str, Any]) -> None:
    if d.get("status") != "started":
        return
    step = d.get("postprocessor")
    emit(log, logging.INFO if step == "Merger" else logging.DEBUG, "download.merge", step=step)


def logged_download(
    url: str, params: dict[str, Any], send: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    """`download` with its log lines, and `send` fed the progress at most once a
    second. Builds the hooks where it runs, because `spawn` pickles arguments."""
    progress_log = logs.ProgressLog()
    last_send: float | None = None
    # Subtitles and the chat replay download first, through the same hook.
    # Progress counts only the media, which starts once the format is logged.
    media = False

    def on_format(info: dict[str, Any]) -> None:
        nonlocal media
        media = True
        _log_format(info)

    def on_progress(d: dict[str, Any]) -> None:
        nonlocal last_send
        if not media:
            return
        progress_log(d)
        if send is None or d.get("status") != "downloading":
            return
        now = time.monotonic()
        if last_send is None or now - last_send >= PROGRESS_SEND_SECONDS:
            last_send = now
            send(logs.progress(d))

    params = {**params, "progress_hooks": [on_progress], "postprocessor_hooks": [_on_postprocessor]}
    return download(url, params, on_format=on_format)


def _child(
    url: str,
    params: dict[str, Any],
    out: str,
    level: str,
    fields: dict[str, Any],
    progress: Connection,
) -> None:
    logs.configure(level)
    with logs.context(**fields):
        try:
            result = {"info": logged_download(url, params, progress.send)}
        except BaseException as e:  # noqa: BLE001 - reported to the parent, which decides
            result = {"error": f"{type(e).__name__}: {e}"}
    progress.close()
    Path(out).write_text(json.dumps(result))
    if "error" in result:
        raise SystemExit(1)


async def pump_progress(
    receive: Connection, report: Report, *, every: float = PROGRESS_WRITE_SECONDS
) -> None:
    """Write the latest progress from the child at most once per `every`
    seconds, until the child closes the pipe. Reads on the event loop, so a
    download holds no thread for it."""
    loop = asyncio.get_running_loop()
    fd = receive.fileno()
    latest: dict[str, Any] | None = None
    closed = False
    arrived = asyncio.Event()

    def readable() -> None:
        nonlocal latest, closed
        try:
            latest = receive.recv()
        except EOFError, OSError:
            closed = True
            loop.remove_reader(fd)
        arrived.set()

    loop.add_reader(fd, readable)
    try:
        while True:
            await arrived.wait()
            arrived.clear()
            if latest is not None:
                update, latest = latest, None
                try:
                    await report(update)
                except Exception as e:
                    # Progress is for display. The download goes on without it.
                    emit(log, logging.WARNING, "job.progress_failed", reason=f"{e}")
            if closed:
                return
            await asyncio.sleep(every)
    finally:
        if not closed:
            loop.remove_reader(fd)


async def in_child(url: str, params: dict[str, Any], report: Report) -> dict[str, Any]:
    """Run one download in its own process.

    A Python thread cannot be stopped from outside, and a cancel or a hung
    download needs a process kill.
    """
    ctx = multiprocessing.get_context("spawn")
    level = logging.getLevelName(logging.getLogger("savetube").getEffectiveLevel())
    receive, send = ctx.Pipe(duplex=False)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "result.json"
        proc = ctx.Process(
            target=_child,
            args=(url, params, str(out), level, logs.current(), send),
            daemon=True,
        )
        proc.start()
        # The child holds the only write end, so its exit ends the pump.
        send.close()
        pump = asyncio.create_task(pump_progress(receive, report))
        try:
            await asyncio.to_thread(proc.join)
        except asyncio.CancelledError:
            proc.kill()
            await asyncio.to_thread(proc.join)
            raise
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            receive.close()
        result = json.loads(out.read_text()) if out.exists() else {}
        if proc.exitcode != 0 or "info" not in result:
            raise DownloadFailed(result.get("error") or f"exit code {proc.exitcode}")
        return result["info"]


async def in_process(url: str, params: dict[str, Any], report: Report) -> dict[str, Any]:
    """A recorder's download. Its pod is the process, so progress only logs."""
    return await asyncio.to_thread(logged_download, url, params)


async def _set_video_state(session: AsyncSession, video_id: str, state: VideoState) -> None:
    video = await session.get(Video, video_id)
    # A video stored by an earlier job stays complete through a re-download.
    if video is not None and video.state is not VideoState.COMPLETE:
        video.state = state


async def _describe(session: AsyncSession, video_id: str) -> dict[str, Any]:
    """`channel` and `title` for a line that starts or ends a job."""
    row = (
        await session.execute(
            select(Video.title, Channel.name)
            .join(Channel, Channel.id == Video.channel_id)
            .where(Video.id == video_id)
        )
    ).one_or_none()
    if row is None:
        return {"channel": None, "title": None}
    return {"channel": row.name, "title": logs.cut(row.title, TITLE_CHARS)}


async def reclaim_stalled(
    sessionmaker: async_sessionmaker, settings: Settings, *, stalled: Stalled, finish: Finish
) -> None:
    """Fail the jobs of a dead worker. Their rows would stay running and hold
    download slots forever. The next poll queues each video again."""
    for queue_job_id, job_id in await stalled():
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            if job is not None and job.state in (JobState.QUEUED, JobState.RUNNING):
                described = await _describe(session, job.video_id) if job.video_id else {}
                emit(
                    log,
                    logging.WARNING,
                    "job.reclaimed",
                    job=job_id,
                    video=job.video_id,
                    title=described.get("title"),
                    reason=STALLED_LOG,
                )
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


@dataclass
class Dispatched:
    """One dispatch, for the scheduler's log lines."""

    started: list[int]
    # Archive jobs still waiting for the dispatcher.
    waiting: int
    running: int
    max_concurrent: int
    # When the download window next opens. None inside it.
    opens: datetime | None
    # Jobs whose queue job ended before the worker marked the row.
    requeued: list[int]
    rate_limit: int | None
    window: str | None


async def dispatch(session: AsyncSession, now: datetime, enqueue: Enqueue) -> Dispatched:
    """Put the oldest waiting archive jobs on the queue, one per free slot, and
    only inside the download window. Commits.

    A waiting job is a queued archive job with no queue job. A dispatched job
    holds its slot until a worker takes it or hands it back.
    """
    await lock(session)
    limits = await load(session)
    queued = (Job.kind == JobKind.ARCHIVE) & (Job.state == JobState.QUEUED)
    # A queue job that ended before the worker marked the row, such as an
    # error or a stopped worker, would hold its slot for good.
    requeued = list(
        await session.scalars(
            update(Job)
            .where(queued, Job.queue_job_id.is_not(None), Job.queue_job_id.not_in(_LIVE_QUEUE_JOBS))
            .values(queue_job_id=None)
            .returning(Job.id)
        )
    )
    opens = next_window_start(now, limits.window_start, limits.window_end)
    await session.execute(
        update(Job)
        .where(queued, Job.queue_job_id.is_(None), Job.deferred_until.is_distinct_from(opens))
        .values(deferred_until=opens)
    )
    started: list[int] = []
    busy = await running(session)
    if opens is None:
        dispatched = await session.scalar(
            select(func.count()).select_from(Job).where(queued, Job.queue_job_id.is_not(None))
        )
        free = limits.max_concurrent - busy - dispatched
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
                emit(log, logging.ERROR, "dispatch.enqueue_failed", job=job_id, exc_info=True)
                break
            await session.execute(
                update(Job).where(Job.id == job_id).values(queue_job_id=queue_job_id)
            )
            started.append(job_id)
    left = await session.scalar(
        select(func.count()).select_from(Job).where(queued, Job.queue_job_id.is_(None))
    )
    await session.commit()
    window = None
    if limits.window_start is not None and limits.window_end is not None:
        window = f"{limits.window_start:%H:%M}-{limits.window_end:%H:%M}"
    return Dispatched(
        started=started,
        waiting=left,
        running=busy,
        max_concurrent=limits.max_concurrent,
        opens=opens,
        requeued=requeued,
        rate_limit=limits.rate_limit_bytes,
        window=window,
    )


_RECORDED = "savetube_recorded"


def recorded(e: BaseException) -> bool:
    """Whether `run` already wrote the failure to the job row and logged it."""
    return getattr(e, _RECORDED, False)


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
        video_id = job.video_id
        if not live:
            now = datetime.now(UTC)
            limits = await admit(session, now)
            if limits is None:
                # A core UPDATE: the row loaded above may predate the
                # dispatcher's commit of queue_job_id.
                await session.execute(update(Job).where(Job.id == job_id).values(queue_job_id=None))
                await session.commit()
                current = await load(session)
                closed = next_window_start(now, current.window_start, current.window_end)
                emit(
                    log,
                    logging.INFO,
                    "job.returned",
                    job=job_id,
                    video=video_id,
                    reason="window" if closed is not None else "slots",
                )
                return
            ratelimit = limits.rate_limit_bytes
        channel = await settings_channel(session, video_id)
        use_cookies = channel is not None and channel.use_cookies
        fmt = channel.format if channel is not None else DEFAULT_FORMAT
        shorts = await from_shorts_tab(session, video_id)
        job.deferred_until = None
        job.state = JobState.RUNNING
        job.attempts += 1
        job.started_at = datetime.now(UTC)
        await _set_video_state(session, video_id, VideoState.DOWNLOADING)
        if live:
            attempt, limit = job.attempts, None
        else:
            failed = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.video_id == video_id,
                    Job.kind == JobKind.ARCHIVE,
                    Job.state == JobState.FAILED,
                )
            )
            attempt, limit = failed + 1, MAX_ARCHIVE_ATTEMPTS
        described = await _describe(session, video_id)
        await session.commit()

    attempt_label = f"{attempt}/{limit}" if limit else str(attempt)
    started = time.monotonic()
    with logs.context(job=job_id, video=video_id):
        emit(
            log,
            logging.INFO,
            "job.start",
            job=job_id,
            video=video_id,
            **described,
            attempt=attempt_label,
            format_pref=fmt,
            cookies="yes" if use_cookies else "no",
            ratelimit=f"{human_bytes(ratelimit)}/s" if ratelimit else None,
        )

        async def report(progress: dict[str, Any]) -> None:
            async with sessionmaker() as session:
                await session.execute(update(Job).where(Job.id == job_id).values(progress=progress))
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
                info = await runner(video_url(video_id), params, report)

            async with sessionmaker() as session:
                job = await session.get(Job, job_id)
                video = await store(
                    session,
                    job=job,
                    info=info,
                    incoming=incoming,
                    archive_root=settings.archive_root,
                    from_shorts_tab=shorts,
                )
                await session.commit()
            emit(
                log,
                logging.INFO,
                "job.done",
                job=job_id,
                video=video_id,
                **described,
                size=human_bytes(video.size_bytes) if video.size_bytes is not None else None,
                took=human_duration(time.monotonic() - started),
                path=video.dir_path,
            )
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
            final = live or (limit is not None and attempt >= limit)
            emit(
                log,
                logging.ERROR if final else logging.WARNING,
                "job.failed",
                job=job_id,
                video=video_id,
                **described,
                attempt=attempt_label,
                took=human_duration(time.monotonic() - started),
                reason=logs.cut(f"{type(e).__name__}: {e}"),
                # An expected failure is one line. Anything else needs its traceback.
                exc_info=not isinstance(e, DownloadFailed),
            )
            setattr(e, _RECORDED, True)
            raise
