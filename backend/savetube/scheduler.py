"""The single-replica loop: source polls, live detection, recorder Jobs, storage samples.

Only storage sampling runs today. Live detection and source polling are Build
order steps 4 and 5 in docs/plan.md.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import StorageSample
from savetube.storage import disk_usage

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


async def run(settings: Settings) -> None:
    while True:
        try:
            await sample_storage(settings)
        except Exception:
            log.exception("storage sample failed")
        await asyncio.sleep(TICK_SECONDS)
