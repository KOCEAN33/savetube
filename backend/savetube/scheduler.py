"""The single-replica loop: source polls, live detection, recorder Jobs, storage samples.

Live detection is not built yet (docs/plan.md, Build order step 3).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import StorageSample
from savetube.poll import due_sources, poll_source
from savetube.storage import disk_usage
from savetube.ytdl import list_entries

log = logging.getLogger(__name__)

TICK_SECONDS = 60
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
    from savetube.queue import archive

    async def enqueue(job_id: int) -> int:
        return await archive.defer_async(job_id=job_id)

    sessionmaker = default_sessionmaker()
    async with sessionmaker() as session:
        due = await due_sources(session, datetime.now(UTC))
    # One at a time: every listing counts against YouTube's request limits.
    for source_id in due:
        await poll_source(sessionmaker, source_id, settings, lister=list_entries, enqueue=enqueue)


async def _every(name: str, step: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            await step()
        except Exception:
            log.exception("%s failed", name)
        await asyncio.sleep(TICK_SECONDS)


async def run(settings: Settings) -> None:
    from savetube.queue import app

    async with app.open_async():
        # Separate loops, so a long first listing of a big channel never delays
        # the storage sample.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_every("storage sample", lambda: sample_storage(settings)))
            tg.create_task(_every("source poll", lambda: poll_sources(settings)))
