"""Putting waiting archive jobs on the queue within the download limits."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Text, column, func, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession

from savetube.db.models import Job, JobKind, JobState
from savetube.limits import (
    blocked,
    capped,
    concurrency,
    downloaded_today,
    load,
    lock,
    running,
    window_label,
    window_opens,
)
from savetube.logs import emit

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")

# Puts the `job` row on the queue and returns the queue's job id.
Enqueue = Callable[[int], Awaitable[int]]


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
    # Archive downloads today, succeeded or running, and the daily cap.
    today: int = 0
    daily_limit: int | None = None
    # When the daily cap resets. None while it lets a download start.
    capped_until: datetime | None = None


async def dispatch(session: AsyncSession, now: datetime, enqueue: Enqueue) -> Dispatched:
    """Put the oldest waiting archive jobs on the queue, one per free slot, and
    only inside the download window, outside a block pause, under the daily
    cap, and while the user has not paused downloads. Commits.

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
    block_end = blocked(limits, now)
    today = await downloaded_today(session, now)
    cap_end = capped(limits, today, now)
    # The later of the window start, the block's end, and the cap's reset.
    ends = [t for t in (window_opens(limits, now), block_end, cap_end) if t is not None]
    opens = max(ends, default=None)
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
        if limits.daily_limit is not None:
            free = min(free, limits.daily_limit - today - dispatched)
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
    return Dispatched(
        started=started,
        waiting=left,
        running=busy,
        max_concurrent=max_concurrent,
        opens=opens,
        requeued=requeued,
        rate_limit=limits.rate_limit_bytes,
        window=window_label(limits),
        blocked_until=block_end,
        paused=limits.paused,
        today=today,
        daily_limit=limits.daily_limit,
        capped_until=cap_end,
    )
