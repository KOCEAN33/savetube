"""The single-replica loop: source polls, archive dispatch, live detection, recorder
Jobs, storage samples.

Recorder Jobs are not built yet (docs/plan/README.md, Build order step 3).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import StorageSample
from savetube.jobs import dispatch, reclaim_stalled
from savetube.live import Seen, check_channel, watched_channels
from savetube.poll import due_sources, poll_source
from savetube.storage import disk_usage
from savetube.ytdl import fetch_feed, list_entries, list_streams, probe_live, probe_video

log = logging.getLogger(__name__)

TICK_SECONDS = 60
# With max_concurrent 1 a slot sits idle up to this long after each download,
# so it is shorter than the tick. One dispatch is a few small queries.
DISPATCH_SECONDS = 10
SAMPLE_EVERY = timedelta(hours=1)


async def sample_storage(settings: Settings) -> None:
    async with default_sessionmaker()() as session:
        last = await session.scalar(select(func.max(StorageSample.sampled_at)))
        now = datetime.now(UTC)
        if last is not None and now - last < SAMPLE_EVERY:
            return
        total, free = disk_usage(settings.archive_root)
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


async def dispatch_archives() -> None:
    from savetube.queue import enqueue

    async with default_sessionmaker()() as session:
        await dispatch(session, datetime.now(UTC), enqueue)


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
        try:
            await check_channel(
                sessionmaker,
                channel_id,
                fetch_feed=fetch_feed,
                probe=probe_video,
                probe_live=probe_live,
                list_streams=list_streams,
                seen=seen,
            )
        except Exception:
            log.exception("live check of %s failed", channel_id)


async def _every(
    name: str, step: Callable[[], Awaitable[None]], seconds: float = TICK_SECONDS
) -> None:
    while True:
        try:
            await step()
        except Exception:
            log.exception("%s failed", name)
        await asyncio.sleep(seconds)


async def run(settings: Settings) -> None:
    from savetube.queue import app

    async with app.open_async():
        # Separate loops, so a long first listing of a big channel never delays
        # live detection or the storage sample.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_every("storage sample", lambda: sample_storage(settings)))
            tg.create_task(_every("source poll", lambda: poll_sources(settings)))
            tg.create_task(_every("archive dispatch", dispatch_archives, DISPATCH_SECONDS))
            tg.create_task(_every("stalled job reclaim", lambda: reclaim(settings)))
            seen: Seen = {}
            tg.create_task(_every("live detection", lambda: detect_lives(seen)))
