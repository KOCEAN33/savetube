"""External re-encoding: its settings and jobs (docs/plan/encode.md)."""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube.attention import dismissed
from savetube.db.models import EncodeJob, EncodeSettings, EncodeState, FormatWatch, Video
from savetube.encode import PROFILES
from savetube.logs import emit
from savetube.web.api.deps import Session, call_scheduler, log
from savetube.web.api.schemas import _Out

router = APIRouter()

OPEN = (EncodeState.QUEUED, EncodeState.CLAIMED)
# The scheduler deletes one file, so it answers at once.
CANCEL_TIMEOUT = 10


class EncodeSettingsIn(BaseModel):
    enabled: bool
    profile: str


class EncodePages(BaseModel):
    """One page of each job list. Every route answers with the pages asked
    for, so a client keeps the pages it shows."""

    open_offset: int = Field(0, ge=0)
    finished_offset: int = Field(0, ge=0)
    limit: int = Field(10, ge=1, le=100)


Pages = Annotated[EncodePages, Query()]


class EncodeJobOut(_Out):
    id: int
    video_id: str
    title: str
    state: str
    profile: str
    source_size: int
    result_size: int | None
    reason: str | None
    log_tail: str | None
    created_at: datetime
    finished_at: datetime | None
    # A failed job the user dismissed (POST /api/attention/dismiss).
    dismissed: bool


class EncodeJobPage(_Out):
    total: int
    items: list[EncodeJobOut]


class EncodeSummary(_Out):
    # source_size - result_size over succeeded jobs with a result size, and
    # the source_size of those jobs.
    saved_bytes: int
    source_bytes: int
    # The part of saved_bytes whose original still waits in `.originals/`.
    held_bytes: int
    succeeded: int
    # Failed and not dismissed.
    failed: int
    # Queued and claimed.
    open: int


class EncodeOut(EncodeSettingsIn):
    profiles: list[str]
    summary: EncodeSummary
    # Queued and claimed jobs and the failed ones not dismissed, oldest first.
    waiting: EncodeJobPage
    # The other jobs, newest first.
    finished: EncodeJobPage


def _dismissed() -> ColumnElement[bool]:
    return dismissed(EncodeJob.id, "encode_failed")


def _failed() -> ColumnElement[bool]:
    return (EncodeJob.state == EncodeState.FAILED) & ~_dismissed()


def _waiting() -> ColumnElement[bool]:
    """Jobs that wait on the encoder or on a user. The others are finished."""
    return EncodeJob.state.in_(OPEN) | _failed()


async def _summary(session: AsyncSession) -> tuple[EncodeSummary, int, int]:
    """The summary, and the totals of the waiting and finished lists.

    No encode queues for a video that keeps an original, and a succeeded job
    keeps its video from queueing again (savetube.encode.candidates). So the
    original a succeeded job's video keeps is that job's source."""
    saved = (EncodeJob.state == EncodeState.SUCCEEDED) & EncodeJob.result_size.is_not(None)
    saving = EncodeJob.source_size - EncodeJob.result_size

    def total(column: ColumnElement[int], where: ColumnElement[bool]):
        return func.coalesce(func.sum(column).filter(where), 0)

    row = (
        await session.execute(
            select(
                total(saving, saved),
                total(EncodeJob.source_size, saved),
                total(saving, saved & FormatWatch.original_path.is_not(None)),
                func.count().filter(EncodeJob.state == EncodeState.SUCCEEDED),
                func.count().filter(_failed()),
                func.count().filter(EncodeJob.state.in_(OPEN)),
                func.count().filter(~_waiting()),
            ).outerjoin(FormatWatch, FormatWatch.video_id == EncodeJob.video_id)
        )
    ).one()
    saved_bytes, source_bytes, held_bytes, succeeded, failed, open_, finished = row
    summary = EncodeSummary(
        saved_bytes=saved_bytes,
        source_bytes=source_bytes,
        held_bytes=held_bytes,
        succeeded=succeeded,
        failed=failed,
        open=open_,
    )
    return summary, open_ + failed, finished


async def _page(
    session: AsyncSession, total: int, waiting: bool, offset: int, limit: int
) -> EncodeJobPage:
    rows = await session.execute(
        select(EncodeJob, Video.title, _dismissed())
        .join(Video, Video.id == EncodeJob.video_id)
        .where(_waiting() if waiting else ~_waiting())
        .order_by(EncodeJob.id if waiting else EncodeJob.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return EncodeJobPage(
        total=total,
        items=[
            EncodeJobOut(
                title=title,
                dismissed=dismissed_,
                **{
                    f: getattr(job, f)
                    for f in EncodeJobOut.model_fields
                    if f not in ("title", "dismissed")
                },
            )
            for job, title, dismissed_ in rows
        ],
    )


async def _out(session: AsyncSession, pages: EncodePages) -> EncodeOut:
    row = await session.get(EncodeSettings, 1)
    summary, waiting, finished = await _summary(session)
    return EncodeOut(
        enabled=row.enabled,
        profile=row.profile,
        profiles=list(PROFILES),
        summary=summary,
        waiting=await _page(session, waiting, True, pages.open_offset, pages.limit),
        finished=await _page(session, finished, False, pages.finished_offset, pages.limit),
    )


@router.get("/encode")
async def encode_settings(session: Session, pages: Pages) -> EncodeOut:
    return await _out(session, pages)


@router.put("/encode")
async def replace_encode_settings(
    session: Session, body: EncodeSettingsIn, pages: Pages
) -> EncodeOut:
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
    return await _out(session, pages)


@router.post("/encode/jobs/{job_id}/cancel")
async def cancel_encode_job(
    session: Session, request: Request, job_id: int, pages: Pages
) -> EncodeOut:
    """Ask the scheduler to delete the queued job's file. The web pod mounts
    the archive read-only."""

    def failed(status: int, reason: str) -> None:
        emit(
            log, logging.WARNING, "encode.cancel_request_failed", job=job_id, status=status,
            reason=reason,
        )  # fmt: skip

    await call_scheduler(request, f"/encode/{job_id}/cancel", seconds=CANCEL_TIMEOUT, failed=failed)
    return await _out(session, pages)


@router.post("/encode/jobs/{job_id}/requeue")
async def requeue_encode_job(session: Session, job_id: int, pages: Pages) -> EncodeOut:
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
    return await _out(session, pages)
