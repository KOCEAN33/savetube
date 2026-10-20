from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube.db.models import (
    Channel,
    Job,
    JobKind,
    JobState,
    Rule,
    Source,
    StorageSample,
    Video,
    VideoState,
)
from savetube.layout import VideoType
from savetube.web.auth import CurrentUser, User, current_user


async def get_session(request: Request):
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TypeStat(_Out):
    type: VideoType
    bytes: int
    count: int


class ChannelStat(_Out):
    channel_id: str
    name: str
    bytes: int
    count: int


class Summary(_Out):
    total_bytes: int
    video_count: int
    duration_seconds: int
    free_bytes: int | None
    by_type: list[TypeStat]
    by_channel: list[ChannelStat]


class DailyStat(_Out):
    day: date
    bytes: int
    count: int


class ChannelOut(_Out):
    id: str
    handle: str | None
    name: str
    dir_name: str
    video_count: int
    bytes: int


class SourceOut(_Out):
    id: int
    url: str
    kind: str
    channel_id: str | None
    enabled: bool
    poll_interval_minutes: int
    rule_id: int
    last_polled_at: datetime | None
    last_error: str | None


class RuleOut(_Out):
    id: int
    name: str
    types: list[str]
    date_after: date | None
    format: str
    use_cookies: bool
    watch_lives: bool
    max_concurrent: int


class JobOut(_Out):
    id: int
    kind: JobKind
    state: JobState
    video_id: str | None
    attempts: int
    miss_reason: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class VideoOut(_Out):
    id: str
    channel_id: str
    type: VideoType
    title: str
    upload_date: date | None
    duration: int | None
    size_bytes: int | None
    state: str
    completed_at: datetime | None


class VideoDetail(VideoOut):
    info: dict[str, Any] | None


class VideoPage(_Out):
    total: int
    items: list[VideoOut]


@router.get("/me")
async def me(user: CurrentUser) -> User:
    return user


_size = func.coalesce(func.sum(Video.size_bytes), 0)
# Statistics count what sits in the tree, not pending or failed downloads.
_stored = Video.state == VideoState.COMPLETE


@router.get("/stats/summary")
async def summary(session: Session) -> Summary:
    total_bytes, video_count, duration = (
        await session.execute(
            select(_size, func.count(), func.coalesce(func.sum(Video.duration), 0)).where(_stored)
        )
    ).one()
    by_type = await session.execute(
        select(Video.type, _size.label("bytes"), func.count().label("count"))
        .where(_stored)
        .group_by(Video.type)
    )
    by_channel = await session.execute(
        select(
            Channel.id.label("channel_id"),
            Channel.name,
            _size.label("bytes"),
            func.count(Video.id).label("count"),
        )
        .join(Video, (Video.channel_id == Channel.id) & _stored)
        .group_by(Channel.id)
        .order_by(_size.desc())
    )
    free = await session.scalar(
        select(StorageSample.free_bytes).order_by(StorageSample.sampled_at.desc()).limit(1)
    )
    return Summary(
        total_bytes=total_bytes,
        video_count=video_count,
        duration_seconds=duration,
        free_bytes=free,
        by_type=[TypeStat.model_validate(r) for r in by_type],
        by_channel=[ChannelStat.model_validate(r) for r in by_channel],
    )


@router.get("/stats/daily")
async def daily(
    session: Session, days: Annotated[int, Query(ge=1, le=3650)] = 90
) -> list[DailyStat]:
    day = func.date_trunc("day", Video.completed_at).label("day")
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await session.execute(
        select(day, _size.label("bytes"), func.count().label("count"))
        .where(_stored, Video.completed_at >= since)
        .group_by(day)
        .order_by(day)
    )
    return [DailyStat(day=r.day.date(), bytes=r.bytes, count=r.count) for r in rows]


@router.get("/channels")
async def channels(session: Session) -> list[ChannelOut]:
    rows = await session.execute(
        select(
            Channel.id,
            Channel.handle,
            Channel.name,
            Channel.dir_name,
            func.count(Video.id).label("video_count"),
            _size.label("bytes"),
        )
        .outerjoin(Video, (Video.channel_id == Channel.id) & _stored)
        .group_by(Channel.id)
        .order_by(Channel.name)
    )
    return [ChannelOut.model_validate(r) for r in rows]


@router.get("/sources")
async def sources(session: Session) -> list[SourceOut]:
    return [
        SourceOut.model_validate(s)
        for s in await session.scalars(select(Source).order_by(Source.id))
    ]


@router.get("/rules")
async def rules(session: Session) -> list[RuleOut]:
    return [
        RuleOut.model_validate(r) for r in await session.scalars(select(Rule).order_by(Rule.id))
    ]


@router.get("/jobs")
async def jobs(
    session: Session,
    kind: JobKind | None = None,
    state: JobState | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[JobOut]:
    q = select(Job).order_by(Job.id.desc()).limit(limit)
    if kind:
        q = q.where(Job.kind == kind)
    if state:
        q = q.where(Job.state == state)
    return [JobOut.model_validate(j) for j in await session.scalars(q)]


@router.get("/videos")
async def videos(
    session: Session,
    channel_id: str | None = None,
    type: VideoType | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> VideoPage:
    where = []
    if channel_id:
        where.append(Video.channel_id == channel_id)
    if type:
        where.append(Video.type == type)
    if q:
        pattern = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
        where.append(or_(Video.title.ilike(pattern), Video.id == q))
    total = await session.scalar(select(func.count()).select_from(Video).where(*where))
    items = await session.scalars(
        select(Video)
        .where(*where)
        .order_by(Video.upload_date.desc().nulls_last(), Video.id)
        .limit(limit)
        .offset(offset)
    )
    return VideoPage(total=total or 0, items=[VideoOut.model_validate(v) for v in items])


@router.get("/videos/{video_id}")
async def video(session: Session, video_id: str) -> VideoDetail:
    v = await session.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404)
    return VideoDetail.model_validate(v)
