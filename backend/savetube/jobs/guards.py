"""Checks around a running archive download: pause, ownership, YouTube blocks,
and network stalls."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube import logs
from savetube.db.models import DownloadSettings, Job, JobKind, JobState
from savetube.joblog import BLOCKED_LOG, NETWORK_LOG
from savetube.jobs.rows import LOG_TAIL_CHARS, _interrupt, _owner_lost
from savetube.jobs.runners import DownloadFailed
from savetube.limits import backoff, blocked, load, lock
from savetube.logs import emit, human_duration
from savetube.tz import TIMEZONE

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")

# The `log_tail` prefix of a run a network error interrupted that added no bytes
# before it. NETWORK_LOG is the prefix of any network interruption.
NETWORK_STALL_LOG = "network error, no progress"
# A running archive download reads the user's pause this often.
PAUSE_POLL_SECONDS = 5


class Paused(Exception):
    """The user paused archive downloads while this one ran."""


class Disowned(Exception):
    """The job row is no longer this run's. A reclaim took it from a worker it
    read as stalled, and a second run may work in the same directory."""


async def _unless_paused[T](
    sessionmaker: async_sessionmaker, job_id: int, started_at: datetime, work: Awaitable[T]
) -> T:
    """Await `work`, and cancel it when the user pauses archive downloads,
    raising Paused, or when the job row is no longer the run's that set
    `started_at`, raising Disowned. A cancel kills the download child and
    keeps its parts, as a shutdown does."""
    task = asyncio.ensure_future(work)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=PAUSE_POLL_SECONDS)
            if task.done():
                break
            try:
                async with sessionmaker() as session:
                    paused = (await load(session)).paused
                    lost = await _owner_lost(session, job_id, started_at)
            except Exception as e:
                # A database hiccup must not end a download that runs for hours.
                emit(log, logging.WARNING, "job.pause_check_failed", reason=f"{e}")
                continue
            if paused or lost is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                # A download that ended before the cancel keeps its result.
                if task.cancelled():
                    raise Paused if lost is None else Disowned(lost)
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise
    return task.result()


async def _blocked(
    sessionmaker: async_sessionmaker,
    job_id: int,
    e: DownloadFailed,
    disowned: Callable[[AsyncSession], Awaitable[bool]],
) -> bool:
    """Interrupt an archive job YouTube refused, so it counts no attempt and
    keeps its parts, and pause archive downloads. Jobs refused together pause
    once: only the first one past an expired pause lengthens it. A run whose
    row `disowned` finds is no longer its own writes nothing, and this returns
    False."""
    async with sessionmaker() as session:
        await lock(session)
        if await disowned(session):
            return False
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
    return True


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
