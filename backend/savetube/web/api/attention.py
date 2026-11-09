"""The Needs attention card on Home: what fails or waits on the user outside
the page that shows it (docs/plan/frontend.md, Home)."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from savetube import formats
from savetube.attention import record_failed, signed_out
from savetube.db.models import (
    AttentionDismissal,
    Channel,
    CookieProfile,
    EncodeJob,
    EncodeState,
    FormatWatch,
    Job,
    JobKind,
    JobState,
    Source,
    Video,
    VideoState,
)
from savetube.watch import replace_active
from savetube.web.api.deps import Session
from savetube.web.api.jobs import job_reason, resolved_failure

router = APIRouter()

Kind = Literal[
    "archive_failed",
    "record_failed",
    "replace_failed",
    "encode_failed",
    "held",
    "source_failing",
    "cookie_signed_out",
]
# The other kinds leave the card once fixed.
DISMISSABLE: frozenset[Kind] = frozenset(
    {"archive_failed", "record_failed", "replace_failed", "encode_failed", "held"}
)

_ERROR_CHARS = 120


class AttentionOut(BaseModel):
    kind: Kind
    # A job id for a job item, the video id for `held`, the source id for
    # `source_failing`, the profile name for `cookie_signed_out`.
    key: str
    video_id: str | None
    channel_id: str | None
    title: str
    channel_name: str | None
    detail: str | None
    # The full text behind a shortened `detail`.
    log_tail: str | None
    at: datetime | None
    dismissable: bool


class DismissItem(BaseModel):
    kind: Kind
    key: str


class DismissIn(BaseModel):
    items: list[DismissItem] = Field(min_length=1, max_length=500)


def _item(kind: Kind, key: object, **fields: object) -> AttentionOut:
    return AttentionOut(kind=kind, key=str(key), dismissable=kind in DISMISSABLE, **fields)


def _short(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _ERROR_CHARS else flat[: _ERROR_CHARS - 3] + "..."


async def _jobs(session: AsyncSession) -> list[AttentionOut]:
    rows = await session.execute(
        select(Job, Video.title, Video.channel_id, Channel.name)
        .join(Video, Video.id == Job.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .where(
            (
                (Job.kind == JobKind.ARCHIVE)
                & Job.state.in_([JobState.FAILED, JobState.INTERRUPTED])
                & ~resolved_failure()
            )
            | record_failed()
        )
    )
    out = []
    for job, title, channel_id, channel_name in rows:
        if job.kind is JobKind.RECORD and job.miss_reason is not None:
            detail = job.miss_reason.replace("_", " ")
        else:
            detail = job_reason(job.log_tail)
        out.append(
            _item(
                "archive_failed" if job.kind is JobKind.ARCHIVE else "record_failed",
                job.id,
                video_id=job.video_id,
                channel_id=channel_id,
                title=title,
                channel_name=channel_name,
                detail=detail,
                log_tail=job.log_tail,
                at=job.finished_at,
            )
        )
    return out


async def _replaces(session: AsyncSession) -> list[AttentionOut]:
    latest = (
        select(func.max(Job.id))
        .where(Job.kind == JobKind.REPLACE)
        .group_by(Job.video_id)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(Job, Video.title, Video.channel_id, Channel.name)
        .join(Video, Video.id == Job.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .where(Job.id.in_(latest), Job.state == JobState.FAILED)
        .where(Video.state == VideoState.COMPLETE)
    )
    return [
        _item(
            "replace_failed",
            job.id,
            video_id=job.video_id,
            channel_id=channel_id,
            title=title,
            channel_name=channel_name,
            detail=job_reason(job.log_tail),
            log_tail=job.log_tail,
            at=job.finished_at,
        )
        for job, title, channel_id, channel_name in rows
    ]


async def _encodes(session: AsyncSession) -> list[AttentionOut]:
    latest = select(func.max(EncodeJob.id)).group_by(EncodeJob.video_id).scalar_subquery()
    rows = await session.execute(
        select(EncodeJob, Video.title, Video.channel_id, Channel.name)
        .join(Video, Video.id == EncodeJob.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .where(EncodeJob.id.in_(latest), EncodeJob.state == EncodeState.FAILED)
        .where(Video.state == VideoState.COMPLETE)
    )
    return [
        _item(
            "encode_failed",
            job.id,
            video_id=job.video_id,
            channel_id=channel_id,
            title=title,
            channel_name=channel_name,
            detail=job.reason,
            log_tail=job.log_tail,
            at=job.finished_at,
        )
        for job, title, channel_id, channel_name in rows
    ]


async def _held(session: AsyncSession) -> list[AttentionOut]:
    """The replaces only a pick starts: the videos whose watch logs
    `watch.held` instead of queueing (watch._queue)."""
    rows = await session.execute(
        select(FormatWatch, Video, Channel.name)
        .join(Video, Video.id == FormatWatch.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .where(
            Video.state == VideoState.COMPLETE,
            FormatWatch.manual.is_(False),
            func.abs(FormatWatch.vod_duration - FormatWatch.stored_duration) > formats.HOLD_SECONDS,
        )
    )
    out = []
    for watch, video, channel_name in rows:
        up = [p for p in formats.picks(watch.formats or [], video.info or {}).values() if p.above]
        if not up or await replace_active(session, video.id) is not None:
            continue
        diff = watch.vod_duration - watch.stored_duration
        upgrade = " + ".join(
            f"{_codec(p)} ({p.format['format_id']})"
            for p in sorted(up, key=lambda p: p.kind != "video")
        )
        out.append(
            _item(
                "held",
                video.id,
                video_id=video.id,
                channel_id=video.channel_id,
                title=video.title,
                channel_name=channel_name,
                detail=f"{diff:+.1f} s, {upgrade}",
                log_tail=None,
                at=watch.checked_at,
            )
        )
    return out


def _codec(pick: formats.Pick) -> str:
    if pick.kind == "video":
        return formats.video_codec(pick.format.get("vcodec"))[1]
    return formats.audio_codec(pick.format.get("acodec"))[1]


async def _sources(session: AsyncSession) -> list[AttentionOut]:
    # A source without a channel shows in Sources waiting instead.
    rows = await session.execute(
        select(Source, Channel.name)
        .join(Channel, Channel.id == Source.channel_id)
        .where(Source.enabled.is_(True), Source.last_error.is_not(None))
    )
    return [
        _item(
            "source_failing",
            source.id,
            video_id=None,
            channel_id=source.channel_id,
            title=source.title or source.url,
            channel_name=channel_name,
            detail=_short(source.last_error),
            log_tail=source.last_error,
            at=source.last_polled_at,
        )
        for source, channel_name in rows
    ]


async def _cookies(session: AsyncSession) -> list[AttentionOut]:
    rows = await session.scalars(select(CookieProfile).where(signed_out()))
    return [
        _item(
            "cookie_signed_out",
            profile.name,
            video_id=None,
            channel_id=None,
            title=profile.name,
            channel_name=None,
            detail="the account read failed, such as a logged-out cookie file",
            log_tail=None,
            at=profile.account_checked_at,
        )
        for profile in rows
    ]


@router.get("/attention")
async def attention(session: Session) -> list[AttentionOut]:
    dismissed = set(await session.execute(select(AttentionDismissal.kind, AttentionDismissal.key)))
    items = [
        *await _jobs(session),
        *await _replaces(session),
        *await _encodes(session),
        *await _held(session),
        *await _sources(session),
        *await _cookies(session),
    ]
    items = [i for i in items if (i.kind, i.key) not in dismissed]
    items.sort(key=lambda i: i.at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return items


@router.post("/attention/dismiss", status_code=204)
async def dismiss(session: Session, body: DismissIn) -> None:
    refused = sorted({i.kind for i in body.items} - DISMISSABLE)
    if refused:
        raise HTTPException(status_code=422, detail=f"{', '.join(refused)} cannot be dismissed")
    await session.execute(
        insert(AttentionDismissal)
        .values([{"kind": i.kind, "key": i.key} for i in body.items])
        .on_conflict_do_nothing()
    )
    await session.commit()
