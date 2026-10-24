"""Download limits for archive jobs: a concurrency cap, a rate limit, a
daily window in Asia/Seoul, a daily cap, a pause after a YouTube block, and
the user's pause (docs/plan/archive.md, Download limits)."""

from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube.db.models import DownloadSettings, Job, JobKind, JobState
from savetube.tz import TIMEZONE

BLOCK_BACKOFF = timedelta(minutes=30)
# The jobs the limits govern. A replace job downloads a stream like an archive
# job, so it waits for the same slots, window and pauses.
DOWNLOAD_KINDS = (JobKind.ARCHIVE, JobKind.REPLACE)
BLOCK_BACKOFF_MAX = timedelta(hours=12)


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


def window_opens(limits: DownloadSettings, now: datetime) -> datetime | None:
    """`next_window_start` for the stored window, None while it is switched off."""
    if not limits.window_enabled:
        return None
    return next_window_start(now, limits.window_start, limits.window_end)


def window_label(limits: DownloadSettings) -> str | None:
    """The window as `HH:MM-HH:MM`, None when none applies."""
    if not limits.window_enabled or limits.window_start is None or limits.window_end is None:
        return None
    return f"{limits.window_start:%H:%M}-{limits.window_end:%H:%M}"


def day_start(now: datetime) -> datetime:
    """00:00 of `now`'s day in Asia/Seoul, when the daily count resets."""
    return datetime.combine(now.astimezone(TIMEZONE).date(), time(0), TIMEZONE)


def next_day_start(now: datetime) -> datetime:
    return day_start(now) + timedelta(days=1)


# One key shared by every worker and the dispatcher: the slot check and the
# RUNNING mark or the enqueue happen under it, so a slot is never taken twice.
_SLOT_LOCK = 0x5A7E7B0E


async def load(session: AsyncSession) -> DownloadSettings:
    """The stored limits, or the defaults when the row is missing."""
    return await session.get(DownloadSettings, 1) or DownloadSettings(
        id=1, max_concurrent=1, block_count=0, paused=False, window_enabled=False
    )


def backoff(count: int) -> timedelta:
    """The pause after the `count`th block in a row: 30 min, doubling, at most 12 h."""
    return min(BLOCK_BACKOFF * 2 ** (count - 1), BLOCK_BACKOFF_MAX)


def blocked(limits: DownloadSettings, now: datetime) -> datetime | None:
    """When the pause after a YouTube block ends, None when no pause holds."""
    until = limits.blocked_until
    return until if until is not None and until > now else None


def concurrency(limits: DownloadSettings) -> int:
    """`max_concurrent`, or 1 after a block until a download succeeds, so one
    job tests whether YouTube still blocks."""
    return 1 if limits.block_count else limits.max_concurrent


async def lock(session: AsyncSession) -> None:
    """Hold the slot lock until the transaction ends."""
    await session.execute(select(func.pg_advisory_xact_lock(_SLOT_LOCK)))


async def running(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind.in_(DOWNLOAD_KINDS), Job.state == JobState.RUNNING)
    )


async def downloaded_today(session: AsyncSession, now: datetime) -> int:
    """Archive jobs succeeded since 00:00 in Asia/Seoul, plus running ones.

    Counts successes, not starts, so an interrupted or resumed run takes
    nothing from the daily cap.
    """
    return await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.kind == JobKind.ARCHIVE,
            (Job.state == JobState.RUNNING)
            | ((Job.state == JobState.SUCCEEDED) & (Job.finished_at >= day_start(now))),
        )
    )


def capped(limits: DownloadSettings, today: int, now: datetime) -> datetime | None:
    """When the daily cap resets, None when it lets a download start."""
    if limits.daily_limit is None or today < limits.daily_limit:
        return None
    return next_day_start(now)


async def admit(session: AsyncSession, now: datetime) -> DownloadSettings | None:
    """The limits when an archive job may start now, else None.

    Takes the slot lock. The caller marks the job running in the same
    transaction, so the lock covers the check and the mark.
    """
    await lock(session)
    limits = await load(session)
    if limits.paused:
        return None
    if window_opens(limits, now) is not None:
        return None
    if blocked(limits, now) is not None:
        return None
    if (
        limits.daily_limit is not None
        and capped(limits, await downloaded_today(session, now), now) is not None
    ):
        return None
    if await running(session) >= concurrency(limits):
        return None
    return limits
