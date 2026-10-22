"""Reads and writes of the `job` and `video` rows that several steps share."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from savetube import logs
from savetube.config import Settings
from savetube.db.models import Channel, Job, JobState, Video, VideoState

LOG_TAIL_CHARS = 4000
TITLE_CHARS = 80


async def _owner_lost(
    session: AsyncSession, job_id: int, started_at: datetime, *, for_update: bool = False
) -> str | None:
    """Why the job row is no longer the run's that set `started_at`, or None
    while it is. A reclaim sets the row back to queued, and the next run sets
    a new `started_at`. `for_update` holds the row until the caller commits,
    so no reclaim writes between the check and the caller's write."""
    query = select(Job.state, Job.started_at).where(Job.id == job_id)
    if for_update:
        query = query.with_for_update()
    row = (await session.execute(query)).one_or_none()
    if row is None:
        return "the job row is gone"
    if row.state is not JobState.RUNNING:
        return f"the job is {row.state.value}"
    if row.started_at != started_at:
        return "another run started the job"
    return None


async def _set_video_state(session: AsyncSession, video_id: str, state: VideoState) -> None:
    video = await session.get(Video, video_id)
    # A video stored by an earlier job stays complete through a re-download.
    if video is not None and video.state is not VideoState.COMPLETE:
        video.state = state


async def _describe(session: AsyncSession, video_id: str) -> dict[str, Any]:
    """`channel` and `title` for a line that starts or ends a job."""
    row = (
        await session.execute(
            select(Video.title, Channel.name)
            .join(Channel, Channel.id == Video.channel_id)
            .where(Video.id == video_id)
        )
    ).one_or_none()
    if row is None:
        return {"channel": None, "title": None}
    return {"channel": row.name, "title": logs.cut(row.title, TITLE_CHARS)}


def incoming_dir(settings: Settings, video_id: str) -> Path:
    """An archive download's working directory. It is per video, so a retry
    after an interruption finds the parts."""
    return settings.download_root / video_id


async def _interrupt(session: AsyncSession, job: Job, reason: str) -> None:
    """Put a job the worker stopped back to waiting for the dispatcher. A run
    that started becomes its own `interrupted` row, so the history shows it
    and no attempt counts. The job keeps its id and its place in the queue.
    The caller commits."""
    if job.state is JobState.RUNNING:
        session.add(
            Job(
                kind=job.kind,
                state=JobState.INTERRUPTED,
                video_id=job.video_id,
                attempts=1,
                log_tail=reason,
                progress=job.progress,
                started_at=job.started_at,
                finished_at=datetime.now(UTC),
            )
        )
        job.attempts = max(job.attempts - 1, 0)
    job.state = JobState.QUEUED
    job.queue_job_id = None
    job.started_at = None
    job.progress = None
    if job.video_id is not None:
        await _set_video_state(session, job.video_id, VideoState.PENDING)


async def _pin(session: AsyncSession, video_id: str, format_id: str | None) -> None:
    await session.execute(update(Video).where(Video.id == video_id).values(resume_format=format_id))
