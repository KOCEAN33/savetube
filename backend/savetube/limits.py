"""Download limits for archive jobs: a concurrency cap, a rate limit, and a
daily window in Asia/Seoul (docs/plan/archive.md, Download limits)."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube.db.models import DownloadSettings, Job, JobKind, JobState

TIMEZONE = ZoneInfo("Asia/Seoul")


def next_window_start(now: datetime, start: time | None, end: time | None) -> datetime | None:
    """None when a download may start at `now`, else when the window next opens.

    Asia/Seoul has no DST, so adding a day to a local time is exact.
    """
    if start is None or end is None:
        return None
    local = now.astimezone(TIMEZONE)
    t = local.time()
    inside = start <= t < end if start < end else (t >= start or t < end)
    if inside:
        return None
    opens = datetime.combine(local.date(), start, TIMEZONE)
    return opens if opens > local else opens + timedelta(days=1)


# One key shared by every worker and the dispatcher: the slot check and the
# RUNNING mark or the enqueue happen under it, so a slot is never taken twice.
_SLOT_LOCK = 0x5A7E7B0E


async def load(session: AsyncSession) -> DownloadSettings:
    """The stored limits, or the defaults when the row is missing."""
    return await session.get(DownloadSettings, 1) or DownloadSettings(id=1, max_concurrent=1)


async def lock(session: AsyncSession) -> None:
    """Hold the slot lock until the transaction ends."""
    await session.execute(select(func.pg_advisory_xact_lock(_SLOT_LOCK)))


async def running(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == JobKind.ARCHIVE, Job.state == JobState.RUNNING)
    )


async def admit(session: AsyncSession, now: datetime) -> DownloadSettings | None:
    """The limits when an archive job may start now, else None.

    Takes the slot lock. The caller marks the job running in the same
    transaction, so the lock covers the check and the mark.
    """
    await lock(session)
    limits = await load(session)
    if next_window_start(now, limits.window_start, limits.window_end) is not None:
        return None
    if await running(session) >= limits.max_concurrent:
        return None
    return limits
