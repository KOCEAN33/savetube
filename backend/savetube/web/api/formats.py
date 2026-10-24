"""The formats of a stored video on the video page: the format watch of a
live, and the user's probe and pick (docs/plan/live.md, Format watch)."""

import logging
from datetime import datetime
from functools import partial
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube import formats
from savetube.db.models import FormatWatch, Job, JobKind, JobState, Video, VideoState
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
    format_id: str
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
            for f in listed
            if formats.kind(f) is not None
        ],
        replace=None
        if job is None
        else ReplaceOut(
            id=job.id,
            state=job.state,
            format_id=job.format_id,
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
    """Queue a replace with the user's format. It ends the automatic replaces."""
    found = await _watched(session, video_id)
    if found is None:
        raise HTTPException(status_code=404, detail="the video has no format watch")
    watch, video = found
    if body.format_id not in {str(f.get("format_id")) for f in watch.formats or []}:
        raise HTTPException(status_code=404, detail="the VOD does not offer this format")
    if await replace_active(session, video_id) is not None:
        raise HTTPException(status_code=409, detail="a replace is already queued or running")
    if formats.held(watch.vod_duration, watch.stored_duration) and not body.confirm:
        diff = abs(watch.vod_duration - watch.stored_duration)
        raise HTTPException(
            status_code=409,
            detail=f"the VOD and the stored file differ by {diff:.1f} s; confirm to replace",
        )
    watch.manual = True
    job = Job(kind=JobKind.REPLACE, video_id=video_id, format_id=body.format_id, manual=True)
    session.add(job)
    await session.commit()
    emit(
        log,
        logging.INFO,
        "watch.picked",
        job=job.id,
        video=video_id,
        format=body.format_id,
        confirmed="yes" if body.confirm else "no",
    )
    return await _out(session, watch, video)
