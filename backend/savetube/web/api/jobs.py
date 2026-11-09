import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from savetube.db.models import Channel, Job, JobKind, JobState, Video, VideoState
from savetube.joblog import (
    BLOCKED_LOG,
    NETWORK_LOG,
    NO_MEMBER,
    PROFILE_BLOCKED_LOG,
    SHUTDOWN_LOG,
    STALLED_LOG,
)
from savetube.limits import DISPATCH_ORDER
from savetube.logs import emit
from savetube.web.api.deps import Session, _of_channel, log
from savetube.web.api.schemas import _Out
from savetube.ytdl import members_only, network_error, strip_noise, youtube_block

router = APIRouter()

_REASON_CHARS = 120


def job_reason(log_tail: str | None) -> str | None:
    """One plain line for a finished job's log_tail."""
    if not log_tail:
        return None
    text = log_tail.lower()
    if log_tail.startswith(PROFILE_BLOCKED_LOG):
        return PROFILE_BLOCKED_LOG
    if log_tail.startswith(BLOCKED_LOG) or youtube_block(log_tail):
        return BLOCKED_LOG
    if log_tail.startswith(STALLED_LOG):
        return "worker stopped"
    if log_tail.startswith(SHUTDOWN_LOG):
        return "worker shut down"
    if NO_MEMBER in log_tail or members_only(log_tail):
        return "members-only, no member cookie profile"
    if "integrity check failed" in text:
        check = text.split("integrity check failed:", 1)[-1].split()
        return f"integrity check failed: {check[0]}" if check else "integrity check failed"
    if log_tail.startswith(NETWORK_LOG) or network_error(log_tail):
        return NETWORK_LOG
    line = strip_noise(log_tail)
    return line if len(line) <= _REASON_CHARS else line[: _REASON_CHARS - 3] + "..."


def resolved_failure() -> ColumnElement[bool]:
    """A failed or interrupted archive run a later run of its video fixed: a
    success, or for an interrupted run any later failure. The query must join
    Video on Job.video_id."""
    later = aliased(Job)
    resolved = exists().where(
        later.video_id == Job.video_id,
        later.kind == JobKind.ARCHIVE,
        or_(
            later.state == JobState.SUCCEEDED,
            and_(Job.state == JobState.INTERRUPTED, later.state == JobState.FAILED),
        ),
        later.finished_at > Job.finished_at,
    )
    return and_(
        Job.kind == JobKind.ARCHIVE,
        Job.state.in_([JobState.FAILED, JobState.INTERRUPTED]),
        # No run fixes a video YouTube no longer serves.
        or_(resolved, Video.state == VideoState.GONE),
    )


class JobOut(_Out):
    id: int
    kind: JobKind
    state: JobState
    video_id: str | None
    # The video's title and its uploader channel, null for a job without a video.
    title: str | None
    channel_id: str | None
    channel_name: str | None
    # The run's error or interruption text, and one plain line made from it.
    log_tail: str | None
    reason: str | None
    attempts: int
    miss_reason: str | None
    scheduled_at: datetime | None
    deferred_until: datetime | None
    # A running download's current stream, written every few seconds:
    # {format, pct, done, total, speed, eta}. pct is null without a total.
    # After the last byte, {step}: merging, verifying, or audio.
    progress: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # A failed archive job whose video POST /api/jobs/{id}/retry queues again.
    retryable: bool
    # The video's state, `gone` once YouTube no longer serves it.
    video_state: VideoState | None


def _retryable() -> ColumnElement[bool]:
    """A failed archive job, the newest job of its video, whose video is not
    stored or deleted. Past its attempts, polls wait days between retries."""
    later = aliased(Job)
    return and_(
        Job.kind == JobKind.ARCHIVE,
        Job.state == JobState.FAILED,
        Video.state.not_in(
            [VideoState.COMPLETE, VideoState.STAGED, VideoState.DELETED, VideoState.GONE]
        ),
        # An interrupted row records a run of an earlier job, so it is no newer job.
        ~exists().where(
            later.video_id == Job.video_id,
            later.id > Job.id,
            later.state != JobState.INTERRUPTED,
        ),
    )


@router.get("/jobs")
async def jobs(
    session: Session,
    response: Response,
    kind: JobKind | None = None,
    # Repeat the parameter for several states.
    state: Annotated[list[JobState] | None, Query()] = None,
    channel_id: str | None = None,
    # Drop a failed or interrupted archive run once a later run of its video
    # succeeded, and an interrupted one once a later run of its video ended.
    unresolved: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    # newest: latest first. dispatch: the order the worker takes them, running
    # first, then jobs on the procrastinate queue, then the dispatcher's order.
    order: Literal["newest", "dispatch"] = "newest",
) -> list[JobOut]:
    """A page of jobs. X-Total-Count counts every matching job, so the body stays a list."""
    if order == "dispatch":
        by = (Job.state != JobState.RUNNING, Job.queue_job_id.is_(None), *DISPATCH_ORDER)
    else:
        by = (Job.id.desc(),)
    where = []
    if kind:
        where.append(Job.kind == kind)
    if state:
        where.append(Job.state.in_(state))
    if channel_id:
        where.append(_of_channel(channel_id))
    if unresolved:
        where.append(~resolved_failure())
    total = await session.scalar(
        select(func.count())
        .select_from(Job)
        .outerjoin(Video, Video.id == Job.video_id)
        .where(*where)
    )
    response.headers["X-Total-Count"] = str(total or 0)
    q = (
        select(Job, Video.title, Video.channel_id, Channel.name, Video.state, _retryable())
        .outerjoin(Video, Video.id == Job.video_id)
        .outerjoin(Channel, Channel.id == Video.channel_id)
        .where(*where)
        .order_by(*by)
        .limit(limit)
        .offset(offset)
    )
    own = JobOut.model_fields.keys() - {
        "title",
        "channel_id",
        "channel_name",
        "reason",
        "retryable",
        "video_state",
    }
    return [
        JobOut(
            **{f: getattr(j, f) for f in own},
            title=title,
            channel_id=cid,
            channel_name=name,
            reason=job_reason(j.log_tail),
            retryable=bool(retryable),
            video_state=video_state,
        )
        for j, title, cid, name, video_state, retryable in await session.execute(q)
    ]


@router.post("/jobs/{job_id}/retry")
async def retry_job(session: Session, job_id: int) -> dict[str, int]:
    """Queue a new archive job for a retryable job's video. Returns its id."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    ok = await session.scalar(
        select(_retryable())
        .select_from(Job)
        .join(Video, Video.id == Job.video_id)
        .where(Job.id == job_id)
    )
    if not ok:
        raise HTTPException(status_code=409, detail="the job is not a retryable failure")
    video = await session.get(Video, job.video_id)
    video.state = VideoState.PENDING
    new = Job(kind=JobKind.ARCHIVE, video_id=job.video_id)
    session.add(new)
    await session.commit()
    emit(log, logging.INFO, "job.retried", job=job_id, video=job.video_id, new_job=new.id)
    return {"job_id": new.id}


@router.get("/jobs/counts")
async def job_counts(
    session: Session, kind: JobKind | None = None, channel_id: str | None = None
) -> dict[JobState, int]:
    """The number of jobs in each state, every state present."""
    q = select(Job.state, func.count()).group_by(Job.state)
    if kind:
        q = q.where(Job.kind == kind)
    if channel_id:
        q = q.outerjoin(Video, Video.id == Job.video_id).where(_of_channel(channel_id))
    counts = dict.fromkeys(JobState, 0)
    counts.update({state: n for state, n in await session.execute(q)})
    return counts
