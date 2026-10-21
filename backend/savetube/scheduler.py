"""The single-replica loop: source polls, archive dispatch, live detection, recorder
Jobs, storage samples.

Recorder Jobs are not built yet (docs/plan/README.md, Build order step 3).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import StorageSample
from savetube.jobs import Dispatched, clean_incoming, dispatch, reclaim_stalled
from savetube.limits import TIMEZONE
from savetube.live import Seen, check_logged, watched_channels
from savetube.logs import emit, human_bytes
from savetube.poll import due_sources, poll_source
from savetube.storage import disk_usage
from savetube.ytdl import fetch_feed, list_entries, list_streams, probe_live, probe_video

log = logging.getLogger(__name__)

TICK_SECONDS = 60
# With max_concurrent 1 a slot sits idle up to this long after each download,
# so it is shorter than the tick. One dispatch is a few small queries.
DISPATCH_SECONDS = 10
SAMPLE_EVERY = timedelta(hours=1)
SUMMARY_SECONDS = 600
# Partial directories age in days, so an hourly look is enough.
CLEANUP_SECONDS = 3600


async def sample_storage(settings: Settings) -> None:
    async with default_sessionmaker()() as session:
        last = await session.scalar(select(func.max(StorageSample.sampled_at)))
        now = datetime.now(UTC)
        if last is not None and now - last < SAMPLE_EVERY:
            return
        total, free = disk_usage(settings.archive_root)
        emit(log, logging.DEBUG, "storage.sample", free=human_bytes(free), total=human_bytes(total))
        session.add(StorageSample(sampled_at=now, total_bytes=total, free_bytes=free))
        await session.commit()


async def poll_sources(settings: Settings) -> None:
    from savetube.queue import app

    async def cancel(queue_job_id: int) -> bool:
        return await app.job_manager.cancel_job_by_id_async(queue_job_id)

    sessionmaker = default_sessionmaker()
    async with sessionmaker() as session:
        due = await due_sources(session, datetime.now(UTC))
    # One at a time: every listing counts against YouTube's request limits.
    for source_id in due:
        await poll_source(sessionmaker, source_id, settings, lister=list_entries, cancel=cancel)


class DispatchLog:
    """Logs a dispatch that changes something: jobs sent, a new reason to wait,
    and a summary every 10 minutes while a job waits or runs."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._reason: str | None = None
        self._summary_at: float | None = None

    def __call__(self, d: Dispatched) -> None:
        if d.requeued:
            emit(
                log,
                logging.WARNING,
                "dispatch.requeued",
                jobs=",".join(map(str, d.requeued)),
                reason="queue job ended before the worker marked the row",
            )
        if d.started:
            emit(
                log,
                logging.INFO,
                "dispatch.sent",
                jobs=",".join(map(str, d.started)),
                running=d.running,
                max=d.max_concurrent,
                waiting=d.waiting,
            )
        reason = None
        if d.waiting:
            reason = "window" if d.opens is not None else "slots"
        if reason != self._reason:
            self._reason = reason
            if reason == "window":
                emit(
                    log,
                    logging.INFO,
                    "dispatch.waiting",
                    reason=reason,
                    opens=d.opens.astimezone(TIMEZONE).isoformat(timespec="minutes"),
                    waiting=d.waiting,
                )
            elif reason == "slots":
                emit(
                    log,
                    logging.INFO,
                    "dispatch.waiting",
                    reason=reason,
                    running=d.running + len(d.started),
                    max=d.max_concurrent,
                    waiting=d.waiting,
                )
        now = self._clock()
        if not (d.waiting or d.running or d.started):
            self._summary_at = None
        elif self._summary_at is None or now - self._summary_at >= SUMMARY_SECONDS:
            self._summary_at = now
            emit(
                log,
                logging.INFO,
                "queue.summary",
                waiting=d.waiting,
                running=d.running,
                max=d.max_concurrent,
                window=d.window or "none",
                open="no" if d.opens is not None else "yes",
                ratelimit=f"{human_bytes(d.rate_limit)}/s" if d.rate_limit else "none",
            )


async def dispatch_archives(report: DispatchLog) -> None:
    from savetube.queue import enqueue

    async with default_sessionmaker()() as session:
        report(await dispatch(session, datetime.now(UTC), enqueue))


async def reclaim(settings: Settings) -> None:
    from procrastinate.jobs import Status

    from savetube.queue import ARCHIVE_QUEUE, app

    async def stalled() -> list[tuple[int, int]]:
        # A worker without a heartbeat for 30 s, procrastinate's default and
        # the worker's own stalled_worker_timeout.
        found = await app.job_manager.get_stalled_jobs(queue=ARCHIVE_QUEUE, task_name="archive")
        return [(j.id, j.task_kwargs["job_id"]) for j in found if j.id is not None]

    async def finish(queue_job_id: int) -> None:
        await app.job_manager.finish_job_by_id_async(
            queue_job_id, status=Status.FAILED, delete_job=False
        )

    await reclaim_stalled(default_sessionmaker(), settings, stalled=stalled, finish=finish)


async def detect_lives(seen: Seen) -> None:
    sessionmaker = default_sessionmaker()
    async with sessionmaker() as session:
        channels = await watched_channels(session)
    # Every watched channel costs a Live tab listing and a `/live` extraction
    # per tick. The guest limit is near 300 requests an hour, so this caps the
    # number of watched channels.
    for channel_id in channels:
        await check_logged(
            sessionmaker,
            channel_id,
            fetch_feed=fetch_feed,
            probe=probe_video,
            probe_live=probe_live,
            list_streams=list_streams,
            seen=seen,
        )


async def _every(
    name: str, step: Callable[[], Awaitable[None]], seconds: float = TICK_SECONDS
) -> None:
    while True:
        try:
            await step()
        except Exception:
            emit(log, logging.ERROR, "loop.failed", loop=name, exc_info=True)
        await asyncio.sleep(seconds)


async def run(settings: Settings) -> None:
    from savetube.queue import app

    emit(
        log,
        logging.INFO,
        "scheduler.start",
        tick=f"{TICK_SECONDS}s",
        dispatch=f"{DISPATCH_SECONDS}s",
    )
    async with app.open_async():
        # Separate loops, so a long first listing of a big channel never delays
        # live detection or the storage sample.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_every("storage sample", lambda: sample_storage(settings)))
            tg.create_task(_every("source poll", lambda: poll_sources(settings)))
            report = DispatchLog()
            tg.create_task(
                _every("archive dispatch", lambda: dispatch_archives(report), DISPATCH_SECONDS)
            )
            tg.create_task(_every("stalled job reclaim", lambda: reclaim(settings)))
            tg.create_task(
                _every(
                    "incoming cleanup",
                    lambda: clean_incoming(default_sessionmaker(), settings),
                    CLEANUP_SECONDS,
                )
            )
            seen: Seen = {}
            tg.create_task(_every("live detection", lambda: detect_lives(seen)))
