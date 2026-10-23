"""Recorder Jobs for queued record jobs, started a few minutes before the
stream (docs/plan/live.md, Recording and the VOD)."""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube.db.models import Job, JobKind, JobState, MissReason, VideoState
from savetube.jobs.rows import _set_video_state
from savetube.logs import emit

log = logging.getLogger(__name__)

LEAD = timedelta(minutes=5)

# The recorder Jobs in the namespace: job id to whether the Job failed.
ListRecorders = Callable[[], Awaitable[dict[int, bool]]]
# Creates the recorder Job of a job id and its video id, and returns the Job's name.
CreateRecorder = Callable[[int, str], Awaitable[str]]


async def start_recorders(
    sessionmaker: async_sessionmaker,
    now: datetime,
    *,
    recorders: ListRecorders,
    create: CreateRecorder,
) -> None:
    """Create a recorder Job for each queued record job due within LEAD that has
    none. A stream found live, or one past its scheduled start, is due now. A
    recorder Job that failed every retry leaves its row open, which then
    becomes missed."""
    existing = await recorders()
    async with sessionmaker() as session:
        due = (
            await session.execute(
                select(Job.id, Job.video_id, Job.scheduled_at)
                .where(
                    Job.kind == JobKind.RECORD,
                    Job.state == JobState.QUEUED,
                    or_(Job.scheduled_at.is_(None), Job.scheduled_at <= now + LEAD),
                )
                .order_by(Job.id)
            )
        ).all()
    for job_id, video_id, scheduled_at in due:
        if job_id in existing:
            continue
        # One refused create must not keep the other streams from recording.
        try:
            name = await create(job_id, video_id)
        except Exception:
            emit(
                log,
                logging.ERROR,
                "recorder.create_failed",
                job=job_id,
                video=video_id,
                exc_info=True,
            )
            continue
        emit(
            log,
            logging.INFO,
            "recorder.created",
            job=job_id,
            video=video_id,
            name=name,
            scheduled=scheduled_at.isoformat() if scheduled_at else None,
        )

    failed = [job_id for job_id, failed in existing.items() if failed]
    if not failed:
        return
    async with sessionmaker() as session:
        jobs = list(
            await session.scalars(
                select(Job).where(
                    Job.id.in_(failed),
                    Job.kind == JobKind.RECORD,
                    Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
                )
            )
        )
        for job in jobs:
            job.state = JobState.MISSED
            job.miss_reason = MissReason.RECORDER_FAILED
            job.finished_at = now
            if job.video_id is not None:
                await _set_video_state(session, job.video_id, VideoState.FAILED)
            emit(log, logging.ERROR, "recorder.missed", job=job.id, video=job.video_id)
        await session.commit()
