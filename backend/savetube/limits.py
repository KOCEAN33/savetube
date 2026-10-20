"""Download limits for archive jobs: a concurrency cap, a rate limit, and a
daily window in Asia/Seoul (docs/plan.md, Download limits)."""

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


SLOT_RETRY = timedelta(seconds=60)
# One key shared by every worker: the slot check and the RUNNING mark happen
# under it, so two workers cannot both take the last slot.
_SLOT_LOCK = 0x5A7E7B0E


async def load(session: AsyncSession) -> DownloadSettings:
    """The stored limits, or the defaults when the row is missing."""
    return await session.get(DownloadSettings, 1) or DownloadSettings(id=1, max_concurrent=1)


async def admit(session: AsyncSession, now: datetime) -> tuple[datetime | None, DownloadSettings]:
    """When an archive job must wait until, or None when it may start now.

    Takes a transaction-scoped advisory lock. The caller marks the job running
    in the same transaction, so the lock covers the check and the mark.
    """
    await session.execute(select(func.pg_advisory_xact_lock(_SLOT_LOCK)))
    limits = await load(session)
    opens = next_window_start(now, limits.window_start, limits.window_end)
    if opens is not None:
        return opens, limits
    running = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == JobKind.ARCHIVE, Job.state == JobState.RUNNING)
    )
    if running >= limits.max_concurrent:
        return now + SLOT_RETRY, limits
    return None, limits
