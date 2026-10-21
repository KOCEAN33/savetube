"""One download job, archive or recording, from the `job` row to the tree."""

import asyncio
import contextlib
import json
import logging
import multiprocessing
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from sqlalchemy import Text, column, func, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube import cookies, integrity, logs
from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import (
    Channel,
    DownloadSettings,
    Job,
    JobKind,
    JobState,
    Video,
    VideoState,
)
from savetube.layout import VideoType
from savetube.limits import (
    TIMEZONE,
    admit,
    backoff,
    blocked,
    concurrency,
    load,
    lock,
    next_window_start,
    running,
)
from savetube.logs import emit, human_bytes, human_duration
from savetube.pipeline import from_shorts_tab, prepare_incoming, settings_channel, store
from savetube.poll import MAX_ARCHIVE_ATTEMPTS
from savetube.storage import dir_size
from savetube.ytdl import (
    DEFAULT_FORMAT,
    build_params,
    download,
    network_error,
    video_url,
    youtube_block,
)

log = logging.getLogger(__name__)

LOG_TAIL_CHARS = 4000
TITLE_CHARS = 80
# The child sends progress at most this often, and the parent writes the job
# row at most every PROGRESS_WRITE_SECONDS.
PROGRESS_SEND_SECONDS = 1
PROGRESS_WRITE_SECONDS = 5
# How long the parent waits for the child's last messages after it exits.
PUMP_DRAIN_SECONDS = 10

# Takes what the download child sends: its progress (savetube.logs.progress),
# or an event, a dict with `event` set. `format` carries the chosen
# `format_id`. `stream` carries a downloaded stream's `format_id`, its `size`,
# and the format's exact `filesize` as `expected`, or None.
Report = Callable[[dict[str, Any]], Awaitable[None]]
Runner = Callable[[str, dict[str, Any], Report], Awaitable[dict[str, Any]]]
# Puts the `job` row on the queue and returns the queue's job id.
Enqueue = Callable[[int], Awaitable[int]]
# Queue jobs left running by a dead worker, as (queue job id, `job` id).
Stalled = Callable[[], Awaitable[list[tuple[int, int]]]]
# Ends a queue job as failed, so no worker takes it again.
Finish = Callable[[int], Awaitable[None]]

STALLED_LOG = "worker stopped during the download"
SHUTDOWN_LOG = "worker shut down during the download"
# yt-dlp's text when a pinned format is no longer offered.
FORMAT_GONE = "Requested format is not available"
# A partial directory no queued or running job wants is deleted after this.
ORPHAN_DAYS = 7
BLOCKED_LOG = "blocked by YouTube"
# The `log_tail` prefix of a run a network error interrupted, and of one that
# added no bytes before it.
NETWORK_LOG = "network error"
NETWORK_STALL_LOG = "network error, no progress"
# Network interruptions in a row that added no bytes. A run that adds bytes
# resets the count, so a download that moves on is retried until it ends.
MAX_NETWORK_STALLS = 5
PAUSED_LOG = "paused by user"
# A running archive download reads the user's pause this often.
PAUSE_POLL_SECONDS = 5
NO_MEMBER = "no cookie profile is a confirmed member of the channel"


class DownloadFailed(Exception):
    pass


class Paused(Exception):
    """The user paused archive downloads while this one ran."""


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
        if send is not None:
            send({"event": "format", "format_id": info.get("format_id")})

    def on_progress(d: dict[str, Any]) -> None:
        nonlocal last_send
        if not media:
            return
        progress_log(d)
        if send is None:
            return
        if d.get("status") == "finished" and os.path.isfile(d.get("filename") or ""):
            # A merge deletes the stream files, so their sizes go to the parent now.
            fmt = d.get("info_dict") or {}
            send(
                {
                    "event": "stream",
                    "format_id": fmt.get("format_id"),
                    "size": os.path.getsize(d["filename"]),
                    "expected": fmt.get("filesize"),
                }
            )
            return
        if d.get("status") != "downloading":
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
    # The parent decides how a download ends. A terminal's Ctrl-C reaches the
    # whole process group, and a stop must leave the parts for a resume.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
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
    """Pass every event from the child, and the latest progress at most once
    per `every` seconds, until the child closes the pipe. Reads on the event
    loop, so a download holds no thread for it."""
    loop = asyncio.get_running_loop()
    fd = receive.fileno()
    latest: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    closed = asyncio.Event()
    arrived = asyncio.Event()

    def readable() -> None:
        nonlocal latest
        try:
            message = receive.recv()
        except EOFError, OSError:
            closed.set()
            loop.remove_reader(fd)
        else:
            if "event" in message:
                events.append(message)
            else:
                latest = message
        arrived.set()

    async def write(message: dict[str, Any]) -> None:
        try:
            await report(message)
        except Exception as e:
            # Progress is for display. The download goes on without it.
            emit(log, logging.WARNING, "job.progress_failed", reason=f"{e}")

    loop.add_reader(fd, readable)
    try:
        while True:
            await arrived.wait()
            arrived.clear()
            while events:
                await write(events.pop(0))
            if latest is not None:
                update, latest = latest, None
                await write(update)
            if closed.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(closed.wait(), every)
    finally:
        if not closed.is_set():
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
            # A kill leaves the `.part` files, and the retry resumes them.
            proc.kill()
            await asyncio.to_thread(proc.join)
            pump.cancel()
            raise
        finally:
            # The pump ends at the child's exit, after its last messages.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(pump, PUMP_DRAIN_SECONDS)
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


def incoming_dir(settings: Settings, video_id: str) -> Path:
    """An archive download's working directory. It is per video, so a retry
    after an interruption finds the parts."""
    return settings.incoming_root / video_id


async def _interrupt(session: AsyncSession, job: Job, reason: str) -> None:
    """Put a job the worker stopped back to waiting for the dispatcher. A run
    that started becomes its own `interrupted` row, so the history shows it
    and no attempt counts. The job keeps its id and its place in the queue.
    The caller commits."""
    if job.state is JobState.RUNNING:
        session.add(
            Job(
                kind=job.kind,
                state=JobState.INTERRUPTED,
                video_id=job.video_id,
                attempts=1,
                log_tail=reason,
                progress=job.progress,
                started_at=job.started_at,
                finished_at=datetime.now(UTC),
            )
        )
        job.attempts = max(job.attempts - 1, 0)
    job.state = JobState.QUEUED
    job.queue_job_id = None
    job.started_at = None
    job.progress = None
    if job.video_id is not None:
        await _set_video_state(session, job.video_id, VideoState.PENDING)


async def reclaim_stalled(
    sessionmaker: async_sessionmaker, settings: Settings, *, stalled: Stalled, finish: Finish
) -> None:
    """Interrupt the jobs of a dead worker. Their rows would stay running and
    hold download slots forever. The dispatcher queues each again, and its
    parts stay for the resume."""
    for queue_job_id, job_id in await stalled():
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            if job is not None and job.state in (JobState.QUEUED, JobState.RUNNING):
                described = await _describe(session, job.video_id) if job.video_id else {}
                await _interrupt(session, job, STALLED_LOG)
                await session.commit()
                emit(
                    log,
                    logging.WARNING,
                    "job.interrupted",
                    job=job_id,
                    video=job.video_id,
                    title=described.get("title"),
                    reason=STALLED_LOG,
                )
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
    # When the pause after a YouTube block ends. None when no pause holds.
    blocked_until: datetime | None = None
    # The user's pause, which has no end until the user resumes.
    paused: bool = False


async def dispatch(session: AsyncSession, now: datetime, enqueue: Enqueue) -> Dispatched:
    """Put the oldest waiting archive jobs on the queue, one per free slot, and
    only inside the download window, outside a block pause, and while the user
    has not paused downloads. Commits.

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
    block_end = blocked(limits, now)
    if block_end is not None:
        opens = max(opens, block_end) if opens is not None else block_end
    # The user's pause has no known end, so a waiting job shows none.
    deferred = None if limits.paused else opens
    max_concurrent = concurrency(limits)
    await session.execute(
        update(Job)
        .where(queued, Job.queue_job_id.is_(None), Job.deferred_until.is_distinct_from(deferred))
        .values(deferred_until=deferred)
    )
    started: list[int] = []
    busy = await running(session)
    if opens is None and not limits.paused:
        dispatched = await session.scalar(
            select(func.count()).select_from(Job).where(queued, Job.queue_job_id.is_not(None))
        )
        free = max_concurrent - busy - dispatched
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
        max_concurrent=max_concurrent,
        opens=opens,
        requeued=requeued,
        rate_limit=limits.rate_limit_bytes,
        window=window,
        blocked_until=block_end,
        paused=limits.paused,
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
    tools: integrity.Tools | None = None,
) -> None:
    """Run one job. An archive job outside the download window, with no free
    slot, or while paused, goes back to waiting for the dispatcher. A pause
    while it runs interrupts it. A recording ignores the limits."""
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
                if current.paused:
                    reason = "paused"
                elif blocked(current, now) is not None:
                    reason = "blocked"
                elif next_window_start(now, current.window_start, current.window_end):
                    reason = "window"
                else:
                    reason = "slots"
                emit(log, logging.INFO, "job.returned", job=job_id, video=video_id, reason=reason)
                return
            ratelimit = limits.rate_limit_bytes
        channel = await settings_channel(session, video_id)
        fmt = channel.format if channel is not None else DEFAULT_FORMAT
        shorts = await from_shorts_tab(session, video_id)
        video = await session.get(Video, video_id)
        pinned = video.resume_format if video is not None else None
        # A member profile serves its members-only videos only. Every other
        # download takes the general profile.
        members = video is not None and video.type is VideoType.MEMBERS
        if members:
            profile = await cookies.member_profile(
                session, video.channel_id, cookies.profiles(settings)
            )
        else:
            profile = await cookies.general(session)
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

    workdir = settings.incoming_root / str(job_id) if live else incoming_dir(settings, video_id)
    attempt_label = f"{attempt}/{limit}" if limit else str(attempt)
    started = time.monotonic()
    started_bytes = dir_size(workdir) if workdir.exists() else 0
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
            cookies=profile or "none",
            ratelimit=f"{human_bytes(ratelimit)}/s" if ratelimit else None,
        )

        streams: list[dict[str, Any]] = []

        async def report(message: dict[str, Any]) -> None:
            if message.get("event") == "stream":
                streams.append(message)
                return
            async with sessionmaker() as session:
                if message.get("event") == "format":
                    await _pin(session, video_id, message["format_id"])
                else:
                    await session.execute(
                        update(Job).where(Job.id == job_id).values(progress=message)
                    )
                await session.commit()

        # Any failure past RUNNING must reach the row, or the job stays running and
        # no poll queues the video again.
        try:
            if live:
                prepare_incoming(settings.incoming_root, job_id)
            # Outside the working directory, so the cookie jar never moves into the tree.
            with tempfile.TemporaryDirectory(prefix="savetube-") as private:
                if members and profile is None:
                    # A poll passed the video while a member profile existed.
                    raise DownloadFailed(NO_MEMBER)
                cookiefile = cookies.cookiefile(settings, profile, Path(private))

                def params(format: str) -> dict[str, Any]:
                    p = build_params(
                        workdir,
                        format=format,
                        cookiefile=cookiefile,
                        live=live,
                        ratelimit=ratelimit,
                    )
                    if not live:
                        # A `.part` file left by an interrupted run resumes.
                        p["continuedl"] = True
                    return p

                checks = None
                if live:
                    info = await runner(video_url(video_id), params(fmt), report)
                else:
                    info, checks = await _unless_paused(
                        sessionmaker,
                        _archive(
                            sessionmaker,
                            video_id,
                            workdir,
                            runner=runner,
                            params=params,
                            fmt=fmt,
                            pinned=pinned,
                            report=report,
                            streams=streams,
                            tools=tools or integrity.Tools(),
                        ),
                    )

            async with sessionmaker() as session:
                job = await session.get(Job, job_id)
                video = await store(
                    session,
                    job=job,
                    info=info,
                    incoming=workdir,
                    archive_root=settings.archive_root,
                    from_shorts_tab=shorts,
                )
                if checks is not None:
                    video.resume_format = None
                    video.verified_at = video.completed_at
                    video.verified_checks = checks
                unblocked = 0 if live else await _unblock(session, datetime.now(UTC))
                await session.commit()
            if unblocked:
                emit(log, logging.INFO, "dispatch.unblocked", count=unblocked)
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
        except (asyncio.CancelledError, Paused) as e:
            # A recorder's retry starts from the first segment anyway.
            if live:
                raise
            reason = PAUSED_LOG if isinstance(e, Paused) else SHUTDOWN_LOG
            async with sessionmaker() as session:
                job = await session.get(Job, job_id)
                await _interrupt(session, job, reason)
                await session.commit()
            emit(
                log,
                logging.INFO,
                "job.interrupted",
                job=job_id,
                video=video_id,
                **described,
                took=human_duration(time.monotonic() - started),
                present=human_bytes(dir_size(workdir)) if workdir.exists() else None,
                reason=reason,
            )
            # A pause ends the queue job. The dispatcher queues the job again.
            if isinstance(e, Paused):
                return
            raise
        except Exception as e:
            if not live and isinstance(e, DownloadFailed) and youtube_block(str(e)):
                await _blocked(sessionmaker, job_id, e)
                emit(
                    log,
                    logging.WARNING,
                    "job.interrupted",
                    job=job_id,
                    video=video_id,
                    **described,
                    took=human_duration(time.monotonic() - started),
                    present=human_bytes(dir_size(workdir)) if workdir.exists() else None,
                    reason=BLOCKED_LOG,
                )
                setattr(e, _RECORDED, True)
                raise
            present = dir_size(workdir) if workdir.exists() else 0
            progressed = present > started_bytes
            if (
                not live
                and isinstance(e, DownloadFailed)
                and network_error(str(e))
                and (
                    progressed or await _network_stalls(sessionmaker, video_id) < MAX_NETWORK_STALLS
                )
            ):
                prefix = NETWORK_LOG if progressed else NETWORK_STALL_LOG
                async with sessionmaker() as session:
                    job = await session.get(Job, job_id)
                    await _interrupt(session, job, f"{prefix}: {e}"[:LOG_TAIL_CHARS])
                    await session.commit()
                emit(
                    log,
                    logging.WARNING,
                    "job.interrupted",
                    job=job_id,
                    video=video_id,
                    **described,
                    took=human_duration(time.monotonic() - started),
                    present=human_bytes(present),
                    added=human_bytes(max(present - started_bytes, 0)),
                    reason=prefix,
                )
                setattr(e, _RECORDED, True)
                raise
            # A retry after a real failure starts over, so the parts would
            # otherwise stay on the dataset for good.
            shutil.rmtree(workdir, ignore_errors=True)
            async with sessionmaker() as session:
                job = await session.get(Job, job_id)
                job.state = JobState.FAILED
                job.finished_at = datetime.now(UTC)
                job.log_tail = (str(e) or type(e).__name__)[-LOG_TAIL_CHARS:]
                await _set_video_state(session, video_id, VideoState.FAILED)
                if not live:
                    await _pin(session, video_id, None)
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


async def _unless_paused[T](sessionmaker: async_sessionmaker, work: Awaitable[T]) -> T:
    """Await `work`, and cancel it when the user pauses archive downloads,
    raising Paused. A cancel kills the download child and keeps its parts,
    as a shutdown does."""
    task = asyncio.ensure_future(work)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=PAUSE_POLL_SECONDS)
            if task.done():
                break
            try:
                async with sessionmaker() as session:
                    paused = (await load(session)).paused
            except Exception as e:
                # A database hiccup must not end a download that runs for hours.
                emit(log, logging.WARNING, "job.pause_check_failed", reason=f"{e}")
                continue
            if paused:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                # A download that ended before the cancel keeps its result.
                if task.cancelled():
                    raise Paused
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise
    return task.result()


async def _blocked(sessionmaker: async_sessionmaker, job_id: int, e: DownloadFailed) -> None:
    """Interrupt an archive job YouTube refused, so it counts no attempt and
    keeps its parts, and pause archive downloads. Jobs refused together pause
    once: only the first one past an expired pause lengthens it."""
    async with sessionmaker() as session:
        await lock(session)
        limits = await session.get(DownloadSettings, 1)
        if limits is None:
            limits = DownloadSettings(id=1, max_concurrent=1, block_count=0)
            session.add(limits)
        now = datetime.now(UTC)
        pause: timedelta | None = None
        if blocked(limits, now) is None:
            limits.block_count += 1
            pause = backoff(limits.block_count)
            limits.blocked_until = now + pause
        job = await session.get(Job, job_id)
        await _interrupt(session, job, f"{BLOCKED_LOG}: {e}"[:LOG_TAIL_CHARS])
        await session.commit()
    if pause is not None:
        emit(
            log,
            logging.WARNING,
            "dispatch.blocked",
            until=limits.blocked_until.astimezone(TIMEZONE).isoformat(timespec="minutes"),
            backoff=human_duration(pause.total_seconds()),
            count=limits.block_count,
            job=job_id,
            video=job.video_id,
            reason=logs.cut(str(e)),
        )


async def _network_stalls(sessionmaker: async_sessionmaker, video_id: str) -> int:
    """The video's network interruptions that added no bytes, since its last
    failed or succeeded archive job or network interruption that added bytes.
    Ordered by `finished_at`, because a failed job keeps the id it had before
    its interrupted rows."""
    archive = (Job.video_id == video_id) & (Job.kind == JobKind.ARCHIVE)
    last_end = (
        select(func.max(Job.finished_at))
        .where(
            archive,
            Job.state.in_([JobState.FAILED, JobState.SUCCEEDED])
            | (
                (Job.state == JobState.INTERRUPTED)
                & Job.log_tail.startswith(f"{NETWORK_LOG}: ", autoescape=True)
            ),
        )
        .scalar_subquery()
    )
    async with sessionmaker() as session:
        return await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                archive,
                Job.state == JobState.INTERRUPTED,
                Job.log_tail.startswith(f"{NETWORK_STALL_LOG}: ", autoescape=True),
                (Job.finished_at > last_end) | last_end.is_(None),
            )
        )


async def _unblock(session: AsyncSession, now: datetime) -> int:
    """End the probe after a block: a success resets the count. Returns the
    count it cleared, 0 when none. A success while a pause holds clears
    nothing, because that download started before the block."""
    limits = await load(session)
    if not limits.block_count or blocked(limits, now) is not None:
        return 0
    await lock(session)
    await session.refresh(limits)
    count = limits.block_count
    if not count or blocked(limits, now) is not None:
        return 0
    limits.block_count = 0
    limits.blocked_until = None
    return count


async def _pin(session: AsyncSession, video_id: str, format_id: str | None) -> None:
    await session.execute(update(Video).where(Video.id == video_id).values(resume_format=format_id))


def _present(workdir: Path) -> int | None:
    """The bytes already in the working directory, None when it was missing.
    Creates it either way."""
    found = dir_size(workdir) if workdir.exists() else None
    workdir.mkdir(parents=True, exist_ok=True)
    return found


def _part_bytes(workdir: Path) -> int:
    """The bytes in yt-dlp's unfinished `.part` files, which a resume continues."""
    return sum(f.stat().st_size for f in workdir.rglob("*.part") if f.is_file())


async def _start_over(sessionmaker: async_sessionmaker, video_id: str, workdir: Path) -> None:
    """Delete the parts and the pinned format, leaving an empty directory."""
    shutil.rmtree(workdir, ignore_errors=True)
    _present(workdir)
    async with sessionmaker() as session:
        await _pin(session, video_id, None)
        await session.commit()


def _media_path(info: dict[str, Any]) -> Path | None:
    downloads = info.get("requested_downloads") or []
    path = (downloads[-1] if downloads else info).get("filepath")
    return Path(path) if path else None


async def _archive(
    sessionmaker: async_sessionmaker,
    video_id: str,
    workdir: Path,
    *,
    runner: Runner,
    params: Callable[[str], dict[str, Any]],
    fmt: str,
    pinned: str | None,
    report: Report,
    streams: list[dict[str, Any]],
    tools: integrity.Tools,
) -> tuple[dict[str, Any], list[str]]:
    """Download into `workdir`, resuming its parts, and verify the result.
    Returns the info dict and the checks that passed.

    A download that fails a check starts over from zero once, within the same
    attempt. A second failure raises DownloadFailed.
    """
    url = video_url(video_id)
    for fresh in (False, True):
        streams.clear()
        present = _present(workdir)
        resumed = pinned is not None and present is not None
        if pinned is not None and not resumed:
            await _start_over(sessionmaker, video_id, workdir)
            pinned = None
        if resumed:
            emit(
                log,
                logging.INFO,
                "download.resume",
                format=pinned,
                present=human_bytes(present),
                part=human_bytes(_part_bytes(workdir)),
            )
            try:
                info = await runner(url, params(pinned), report)
            except DownloadFailed as e:
                if FORMAT_GONE not in str(e):
                    raise
                emit(
                    log,
                    logging.WARNING,
                    "download.restart",
                    format=pinned,
                    present=human_bytes(dir_size(workdir)),
                    reason="the pinned format is no longer offered",
                )
                await _start_over(sessionmaker, video_id, workdir)
                streams.clear()
                resumed = False
                info = await runner(url, params(fmt), report)
        else:
            info = await runner(url, params(fmt), report)

        media = _media_path(info) or workdir / "missing"
        try:
            checks = await asyncio.to_thread(
                integrity.verify, media, info, list(streams), resumed=resumed, tools=tools
            )
        except integrity.IntegrityFailed as e:
            emit(
                log,
                logging.WARNING,
                "download.integrity_failed",
                check=e.check,
                **e.numbers,
                resumed="yes" if resumed else "no",
                then="fail" if fresh else "start over",
            )
            await _start_over(sessionmaker, video_id, workdir)
            pinned = None
            if fresh:
                raise DownloadFailed(f"integrity check failed: {e}") from None
            continue
        emit(log, logging.INFO, "download.verified", checks=",".join(checks) or "none")
        return info, checks
    raise AssertionError("unreachable")


async def clean_incoming(sessionmaker: async_sessionmaker, settings: Settings) -> None:
    """Delete partial directories that no queued or running job wants and that
    nothing wrote to for ORPHAN_DAYS, so cancelled or abandoned parts do not
    fill the dataset."""
    root = settings.incoming_root
    if not root.is_dir():
        return
    active = Job.state.in_([JobState.QUEUED, JobState.RUNNING])
    async with sessionmaker() as session:
        wanted = set(
            await session.scalars(select(Job.video_id).where(active, Job.kind == JobKind.ARCHIVE))
        )
        # A recording works in `.incoming/<job id>/`.
        wanted |= {str(i) for i in await session.scalars(select(Job.id).where(active))}
    cutoff = time.time() - ORPHAN_DAYS * 86400
    deleted = []
    for d in root.iterdir():
        if not d.is_dir() or d.name in wanted:
            continue
        newest = max(p.stat().st_mtime for p in [d, *d.rglob("*")])
        if newest >= cutoff:
            continue
        size = dir_size(d)
        shutil.rmtree(d, ignore_errors=True)
        deleted.append(d.name)
        emit(
            log,
            logging.INFO,
            "incoming.deleted",
            dir=d.name,
            size=human_bytes(size),
            idle=human_duration(time.time() - newest),
        )
    if deleted:
        # The pin names formats of parts that are now gone.
        async with sessionmaker() as session:
            await session.execute(
                update(Video).where(Video.id.in_(deleted)).values(resume_format=None)
            )
            await session.commit()
