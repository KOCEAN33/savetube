"""Interrupting the jobs of a dead worker."""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube.config import Settings
from savetube.db.models import Job, JobState
from savetube.joblog import STALLED_LOG
from savetube.jobs.rows import _describe, _interrupt
from savetube.logs import emit

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")

# Queue jobs left running by a dead worker, as (queue job id, `job` id).
Stalled = Callable[[], Awaitable[list[tuple[int, int]]]]
# Ends a queue job as failed, so no worker takes it again.
Finish = Callable[[int], Awaitable[None]]


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
