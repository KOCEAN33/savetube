"""The formats of a stored video on the video page: the format watch of a
live, and the user's probe and pick (docs/plan/live.md, Format watch)."""

import logging
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from savetube import formats
from savetube.db.models import FormatWatch, Job, JobKind, JobState, Video, VideoState
from savetube.limits import lock
from savetube.logs import emit
from savetube.watch import ends_at, replace_active
from savetube.web.api.deps import Session, call_scheduler, log
from savetube.web.api.jobs import job_reason

router = APIRouter()

# The scheduler waits its turn behind the format watch's probe, then probes.
SCHEDULER_TIMEOUT = 120


class FormatRow(BaseModel):
    format_id: str
    kind: Literal["audio", "video"]
    codec: str
    width: int | None
    height: int | None
    fps: float | None
    # kbps: tbr for video, abr for audio.
    bitrate: float | None
    # The exact size, else yt-dlp's estimate. Null for the stored streams.
    size: int | None
    # One of the stored file's streams.
    stored: bool
    # The pick of its kind when it ranks above the stored stream, else the
    # stored stream of that kind.
    recommended: bool
    reason: str | None


class ReplaceOut(BaseModel):
    id: int
    state: JobState
    format_id: str | None
    # A user's pick. A queued automatic replace gives way to a pick.
    manual: bool
    reason: str | None
    created_at: datetime
    finished_at: datetime | None


class FormatWatchOut(BaseModel):
    # Null for a row a user's probe made: no automatic watch.
    started_at: datetime | None
    ends_at: datetime | None
    ended_at: datetime | None
    checked_at: datetime | None
    probe_error: str | None
    # A pick by hand ended the automatic replaces.
    manual: bool
    # The VOD and the stored file differ by more than formats.HOLD_SECONDS,
    # so no replace starts without a confirm.
    held: bool
    vod_duration: float | None
    stored_duration: float | None
    # VOD minus stored, in seconds.
    duration_diff: float | None
    # A replaced file waits aside until the watch ends or `original_until`.
    original_kept: bool
    original_until: datetime | None
    stored: list[FormatRow]
    formats: list[FormatRow]
    # The latest replace job.
    replace: ReplaceOut | None


class PickIn(BaseModel):
    # Counted formats of the last probe, at most one per kind.
    format_ids: list[str] = Field(min_length=1, max_length=2)
    # Required for a held video.
    confirm: bool = False


def _row(
    f: dict[str, Any], k: formats.Kind, *, stored: bool, recommended: dict[str, str], key: str
) -> FormatRow:
    return FormatRow(
        format_id=str(f.get("format_id")),
        kind=k,
        codec=_codec(f, k),
        width=f.get("width") if k == "video" else None,
        height=f.get("height") if k == "video" else None,
        fps=f.get("fps") if k == "video" else None,
        bitrate=f.get("tbr") if k == "video" else f.get("abr"),
        size=None if stored else f.get("filesize") or f.get("filesize_approx"),
        stored=stored,
        recommended=key in recommended,
        reason=recommended.get(key),
    )


def _codec(f: dict[str, Any], kind: formats.Kind) -> str:
    if kind == "video":
        return formats.video_codec(f.get("vcodec"))[1]
    return formats.audio_codec(f.get("acodec"))[1]


async def _out(session: AsyncSession, watch: FormatWatch, video: Video) -> FormatWatchOut:
    listed = watch.formats or []
    info = video.info or {}
    stored = formats.stored_streams(info)
    # Row keys: `stored:<kind>` for a stored stream, the format id otherwise.
    recommended: dict[str, str] = {}
    if listed:
        for kind, pick in formats.picks(listed, info).items():
            key = pick.format["format_id"] if pick.above else f"stored:{kind}"
            recommended[key] = pick.reason
    job = await session.scalar(
        select(Job)
        .where(Job.video_id == video.id, Job.kind == JobKind.REPLACE)
        .order_by(Job.id.desc())
        .limit(1)
    )
    diff = None
    if watch.vod_duration is not None and watch.stored_duration is not None:
        diff = round(watch.vod_duration - watch.stored_duration, 1)
    return FormatWatchOut(
        started_at=watch.started_at,
        ends_at=ends_at(watch),
        ended_at=watch.ended_at,
        checked_at=watch.checked_at,
        probe_error=watch.probe_error,
        manual=watch.manual,
        held=formats.held(watch.vod_duration, watch.stored_duration),
        vod_duration=watch.vod_duration,
        stored_duration=watch.stored_duration,
        duration_diff=diff,
        original_kept=watch.original_path is not None,
        original_until=watch.original_until,
        stored=[
            _row(stored[k], k, stored=True, recommended=recommended, key=f"stored:{k}")
            for k in ("video", "audio")
            if k in stored
        ],
        formats=[
            _row(f, formats.kind(f), stored=False, recommended=recommended, key=str(f["format_id"]))
            for f in formats.best_first(listed)
        ],
        replace=None
        if job is None
        else ReplaceOut(
            id=job.id,
            state=job.state,
            format_id=job.format_id,
            manual=job.manual,
            reason=job_reason(job.log_tail),
            created_at=job.created_at,
            finished_at=job.finished_at,
        ),
    )


async def _watched(session: AsyncSession, video_id: str) -> tuple[FormatWatch, Video] | None:
    watch = await session.get(FormatWatch, video_id)
    if watch is None:
        return None
    return watch, await session.get(Video, video_id)


@router.get("/videos/{video_id}/formats")
async def video_formats(session: Session, video_id: str) -> FormatWatchOut | None:
    """Null for a video without a row: no watch and no probe yet."""
    found = await _watched(session, video_id)
    return None if found is None else await _out(session, *found)


@router.post("/videos/{video_id}/formats/probe")
async def probe_formats(session: Session, request: Request, video_id: str) -> FormatWatchOut:
    """Ask the scheduler to probe the video's formats now. The web pod has no
    cookies, so the scheduler probes."""
    video = await session.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="no such video")
    if video.state is not VideoState.COMPLETE:
        raise HTTPException(status_code=409, detail="the video is not complete")
    await call_scheduler(
        request,
        f"/probe/{quote(video_id, safe='')}",
        seconds=SCHEDULER_TIMEOUT,
        failed=partial(_probe_failed, video_id),
    )
    watch = await session.get(FormatWatch, video_id)
    return await _out(session, watch, video)


def _probe_failed(video_id: str, status: int, reason: str) -> None:
    emit(
        log,
        logging.WARNING,
        "watch.probe_request_failed",
        video=video_id,
        status=status,
        reason=reason,
    )


@router.post("/videos/{video_id}/formats/pick")
async def pick_format(session: Session, video_id: str, body: PickIn) -> FormatWatchOut:
    """Queue a replace with the user's formats. It ends the automatic
    replaces, and cancels an automatic one still queued."""
    found = await _watched(session, video_id)
    if found is None:
        raise HTTPException(status_code=404, detail="the video has no format watch")
    watch, video = found
    listed = {str(f.get("format_id")): f for f in watch.formats or []}
    missing = [i for i in body.format_ids if i not in listed]
    if missing:
        raise HTTPException(status_code=404, detail=f"the VOD does not offer {', '.join(missing)}")
    stored = formats.stored_streams(video.info or {})
    chosen: dict[formats.Kind, str] = {}
    for format_id in body.format_ids:
        k = formats.kind(listed[format_id])
        if k in chosen:
            raise HTTPException(status_code=422, detail=f"pick one {k} format at most")
        if (stored.get(k) or {}).get("format_id") == format_id:
            raise HTTPException(
                status_code=422, detail=f"the stored file already has {k} format {format_id}"
            )
        chosen[k] = format_id
    if formats.held(watch.vod_duration, watch.stored_duration) and not body.confirm:
        diff = abs(watch.vod_duration - watch.stored_duration)
        raise HTTPException(
            status_code=409,
            detail=f"the VOD and the stored file differ by {diff:.1f} s; confirm to replace",
        )
    # The slot lock: no dispatch or worker start between the check and the cancel.
    await lock(session)
    superseded = await replace_active(session, video_id)
    if superseded is not None:
        if superseded.state is JobState.RUNNING or superseded.manual:
            raise HTTPException(status_code=409, detail="a replace is already queued or running")
        await _cancel(session, superseded)
    watch.manual = True
    job = Job(
        kind=JobKind.REPLACE,
        video_id=video_id,
        # Audio first, as watch._queue joins its picks.
        format_id="+".join(chosen[k] for k in ("audio", "video") if k in chosen),
        manual=True,
    )
    session.add(job)
    await session.commit()
    if superseded is not None:
        emit(
            log,
            logging.INFO,
            "job.cancelled",
            job=superseded.id,
            video=video_id,
            format=superseded.format_id,
            reason="a manual pick replaces it",
        )
    emit(
        log,
        logging.INFO,
        "watch.picked",
        job=job.id,
        video=video_id,
        format=job.format_id,
        confirmed="yes" if body.confirm else "no",
    )
    return await _out(session, watch, video)


async def _cancel(session: AsyncSession, job: Job) -> None:
    """Cancel a queued replace and its queue job in the caller's transaction.
    The web opens no procrastinate App, so this calls the SQL function its
    `cancel_job_by_id_async` calls, as the poll's cancel does."""
    if job.queue_job_id is not None:
        cancelled = await session.scalar(
            text("SELECT procrastinate_cancel_job_v1(:id, false, false)"),
            {"id": job.queue_job_id},
        )
        if cancelled is None:
            status = await session.scalar(
                text("SELECT status::text FROM procrastinate_jobs WHERE id = :id"),
                {"id": job.queue_job_id},
            )
            # A worker took it and starts it once it holds the slot lock. A
            # queue job that ended leads to no run.
            if status == "doing":
                raise HTTPException(
                    status_code=409, detail="a replace is already queued or running"
                )
    job.state = JobState.CANCELLED
    job.finished_at = datetime.now(UTC)
