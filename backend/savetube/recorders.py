"""Recorder Jobs: started a few minutes before each stream, and their
outcomes applied to the rows once they end."""

import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube import cookies, watch
from savetube.config import Settings
from savetube.db.models import Job, JobKind, JobState, MissReason, Video, VideoState
from savetube.jobs.recording import (
    NOT_STARTED_LIMIT,
    NOT_STARTED_LOG,
    outcome_path,
    read_outcome,
    workdir,
)
from savetube.jobs.rows import LOG_TAIL_CHARS, _set_video_state
from savetube.layout import VideoType
from savetube.logs import emit, human_bytes
from savetube.pipeline import from_shorts_tab, settings_channel, store
from savetube.recorder_args import Recording
from savetube.ytdl import DEFAULT_FORMAT

log = logging.getLogger(__name__)

LEAD = timedelta(minutes=5)

# Creates the recorder Job of a recording, and returns the Job's name.
CreateRecorder = Callable[[Recording], Awaitable[str]]
# The k8s.JobStatus values of a Job that runs no more pods.
ENDED = ("succeeded", "failed")


async def start_recorders(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    now: datetime,
    existing: dict[int, str],
    *,
    create: CreateRecorder,
) -> None:
    """Create a recorder Job for each queued record job due within LEAD that has
    none, and mark the job running. A stream found live, or one past its
    scheduled start, is due now. `existing` maps the job id of every recorder
    Job to its status."""
    async with sessionmaker() as session:
        due = list(
            await session.scalars(
                select(Job)
                .where(
                    Job.kind == JobKind.RECORD,
                    Job.state == JobState.QUEUED,
                    or_(Job.scheduled_at.is_(None), Job.scheduled_at <= now + LEAD),
                )
                .order_by(Job.id)
            )
        )
        for job in due:
            if job.id in existing or job.video_id is None:
                continue
            rec = await _recording(session, settings, job)
            # One refused create must not keep the other streams from recording.
            try:
                name = await create(rec)
            except Exception:
                emit(
                    log,
                    logging.ERROR,
                    "recorder.create_failed",
                    job=job.id,
                    video=job.video_id,
                    exc_info=True,
                )
                continue
            job.state = JobState.RUNNING
            job.attempts += 1
            job.started_at = now
            await _set_video_state(session, job.video_id, VideoState.DOWNLOADING)
            # Per Job, so a later failure leaves this one's row running.
            await session.commit()
            emit(
                log,
                logging.INFO,
                "recorder.created",
                job=job.id,
                video=job.video_id,
                name=name,
                scheduled=job.scheduled_at.isoformat() if job.scheduled_at else None,
            )


async def _recording(session: AsyncSession, settings: Settings, job: Job) -> Recording:
    video = await session.get(Video, job.video_id)
    channel = await settings_channel(session, job.video_id)
    present = cookies.profiles(settings)
    if video is None:
        profile, member = await cookies.general(session), None
    else:
        profile = await cookies.for_video(session, video, present)
        member = await cookies.member_profile(session, video.channel_id, present)
    return Recording(
        job_id=job.id,
        video_id=job.video_id,
        format=channel.format if channel is not None else DEFAULT_FORMAT,
        profile=profile,
        member_profile=member,
        members=video is not None and video.type is VideoType.MEMBERS,
        wait_until=job.scheduled_at + NOT_STARTED_LIMIT if job.scheduled_at else None,
    )


async def settle_recorders(
    sessionmaker: async_sessionmaker, settings: Settings, now: datetime, existing: dict[int, str]
) -> None:
    """Apply the outcome of each ended recorder Job whose row is still queued
    or running, then delete the outcome file. A running row whose Job is gone,
    such as one deleted by hand, settles as an ended Job. A failure leaves the
    file, and the next call applies it again."""
    ended = {job_id: status for job_id, status in existing.items() if status in ENDED}
    async with sessionmaker() as session:
        open_ids = set(
            await session.scalars(
                select(Job.id).where(
                    Job.id.in_(ended),
                    Job.kind == JobKind.RECORD,
                    Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
                )
            )
        )
        # start_recorders starts rows at `now`, after `existing` was listed,
        # so only a row started before `now` has lost its Job.
        gone = set(
            await session.scalars(
                select(Job.id).where(
                    Job.kind == JobKind.RECORD,
                    Job.state == JobState.RUNNING,
                    Job.id.not_in(existing),
                    Job.started_at < now,
                )
            )
        )
    ended |= dict.fromkeys(gone, "gone")
    open_ids |= gone
    root = settings.incoming_root
    for job_id, status in ended.items():
        if job_id in open_ids:
            await _apply(sessionmaker, settings, now, job_id, status, read_outcome(root, job_id))
        outcome_path(root, job_id).unlink(missing_ok=True)


async def _apply(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    now: datetime,
    job_id: int,
    status: str,
    outcome: dict[str, Any] | None,
) -> None:
    kind = outcome["outcome"] if outcome is not None else None
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if outcome is not None and kind == "recorded":
            video = await _store(session, settings, job, outcome)
            await session.commit()
            emit(
                log,
                logging.INFO,
                "recorder.stored",
                job=job_id,
                video=job.video_id,
                size=human_bytes(video.size_bytes) if video.size_bytes is not None else None,
                path=video.dir_path,
            )
            return
        if outcome is None:
            # A pod that died before writing one, such as one killed for memory.
            _end(job, JobState.MISSED, now, miss_reason=MissReason.RECORDER_FAILED)
        elif kind == "not_started":
            _end(job, JobState.MISSED, now, log_tail=NOT_STARTED_LOG)
        else:
            _end(job, JobState.FAILED, now, log_tail=outcome["error"][-LOG_TAIL_CHARS:])
        if job.video_id is not None:
            await _set_video_state(session, job.video_id, VideoState.FAILED)
        await session.commit()
    shutil.rmtree(workdir(settings.incoming_root, job_id), ignore_errors=True)
    emit(
        log,
        logging.WARNING if kind == "not_started" else logging.ERROR,
        "recorder.missed",
        job=job_id,
        video=job.video_id,
        outcome=kind,
        job_status=status,
    )


def _end(
    job: Job,
    state: JobState,
    now: datetime,
    *,
    log_tail: str | None = None,
    miss_reason: MissReason | None = None,
) -> None:
    job.state = state
    job.finished_at = now
    job.log_tail = log_tail
    job.miss_reason = miss_reason


async def _store(
    session: AsyncSession, settings: Settings, job: Job, outcome: dict[str, Any]
) -> Video:
    """Move the recording into the tree and mark the video complete. The caller commits."""
    checks = outcome["checks"]
    video = await store(
        session,
        job=job,
        info=outcome["info"],
        incoming=workdir(settings.incoming_root, job.id),
        archive_root=settings.archive_root,
        from_shorts_tab=await from_shorts_tab(session, job.video_id),
    )
    await watch.start(session, video, video.completed_at)
    if checks is not None:
        video.resume_format = None
        video.verified_at = video.completed_at
        video.verified_checks = checks
    return video
