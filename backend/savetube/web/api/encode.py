"""External re-encoding: its settings and jobs (docs/plan/encode.md)."""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube.db.models import EncodeJob, EncodeSettings, EncodeState, Video
from savetube.encode import PROFILES
from savetube.logs import emit
from savetube.web.api.deps import Session, call_scheduler, log
from savetube.web.api.schemas import _Out

router = APIRouter()

# Finished jobs the list shows besides the open and failed ones.
RECENT_JOBS = 20
# The scheduler deletes one file, so it answers at once.
CANCEL_TIMEOUT = 10


class EncodeSettingsIn(BaseModel):
    enabled: bool
    profile: str


class EncodeJobOut(_Out):
    id: int
    video_id: str
    title: str
    state: str
    profile: str
    source_size: int
    result_size: int | None
    reason: str | None
    created_at: datetime
    finished_at: datetime | None


class EncodeOut(EncodeSettingsIn):
    profiles: list[str]
    jobs: list[EncodeJobOut]


async def _out(session: AsyncSession) -> EncodeOut:
    row = await session.get(EncodeSettings, 1)
    waiting = (EncodeState.QUEUED, EncodeState.CLAIMED, EncodeState.FAILED)
    base = select(EncodeJob, Video.title).join(Video, Video.id == EncodeJob.video_id)
    rows = [
        *(await session.execute(base.where(EncodeJob.state.in_(waiting)).order_by(EncodeJob.id))),
        *(
            await session.execute(
                base.where(EncodeJob.state.not_in(waiting))
                .order_by(EncodeJob.id.desc())
                .limit(RECENT_JOBS)
            )
        ),
    ]
    return EncodeOut(
        enabled=row.enabled,
        profile=row.profile,
        profiles=list(PROFILES),
        jobs=[
            EncodeJobOut(
                title=title,
                **{f: getattr(job, f) for f in EncodeJobOut.model_fields if f != "title"},
            )
            for job, title in rows
        ],
    )


@router.get("/encode")
async def encode_settings(session: Session) -> EncodeOut:
    return await _out(session)


@router.put("/encode")
async def replace_encode_settings(session: Session, body: EncodeSettingsIn) -> EncodeOut:
    """Turning it off queues no more jobs. Jobs already queued stay for the
    encoder until a user cancels them."""
    if body.profile not in PROFILES:
        raise HTTPException(status_code=422, detail=f"no profile named {body.profile}")
    row = await session.get(EncodeSettings, 1)
    row.enabled, row.profile = body.enabled, body.profile
    await session.commit()
    emit(
        log,
        logging.INFO,
        "settings.encode",
        enabled="on" if body.enabled else "off",
        profile=body.profile,
    )
    return await _out(session)


@router.post("/encode/jobs/{job_id}/cancel")
async def cancel_encode_job(session: Session, request: Request, job_id: int) -> EncodeOut:
    """Ask the scheduler to delete the queued job's file. The web pod mounts
    the archive read-only."""

    def failed(status: int, reason: str) -> None:
        emit(
            log, logging.WARNING, "encode.cancel_request_failed", job=job_id, status=status,
            reason=reason,
        )  # fmt: skip

    await call_scheduler(request, f"/encode/{job_id}/cancel", seconds=CANCEL_TIMEOUT, failed=failed)
    return await _out(session)


@router.post("/encode/jobs/{job_id}/requeue")
async def requeue_encode_job(session: Session, job_id: int) -> EncodeOut:
    """Let a failed job's video be queued again. The scheduler deletes the
    job's files in failed/ at its next look."""
    job = await session.get(EncodeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such encode job")
    if job.state is not EncodeState.FAILED:
        raise HTTPException(status_code=409, detail=f"the job is {job.state.value}")
    job.state = EncodeState.REQUEUED
    await session.commit()
    emit(log, logging.INFO, "encode.requeued", job=job_id, video=job.video_id)
    return await _out(session)
