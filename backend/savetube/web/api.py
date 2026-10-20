from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
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
from savetube.sources import InvalidSourceUrl, Tab
from savetube.sources import parse as parse_source_url
from savetube.web.auth import CurrentUser, User, current_user
from savetube.ytdl import DEFAULT_FORMAT


async def get_session(request: Request):
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]

CSRF_HEADER = "X-Requested-With"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_csrf_header(request: Request) -> None:
    """A cross-origin page cannot send a custom header without a CORS preflight,
    which this app never answers. SameSite=lax alone still lets a sibling
    subdomain POST with the session cookie."""
    if request.method not in _SAFE_METHODS and CSRF_HEADER.lower() not in request.headers:
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")


router = APIRouter(
    prefix="/api", dependencies=[Depends(current_user), Depends(require_csrf_header)]
)


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
    scheduled_at: datetime | None
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


class SourceIn(BaseModel):
    url: str
    # For a bare channel URL: one source per tab.
    tabs: list[Tab] = []
    rule_id: int
    poll_interval_minutes: Annotated[int, Field(ge=10, le=7 * 24 * 60)] = 360
    enabled: bool = True


class SourcePatch(BaseModel):
    enabled: bool | None = None
    rule_id: int | None = None
    poll_interval_minutes: Annotated[int | None, Field(ge=10, le=7 * 24 * 60)] = None


async def _source(session: AsyncSession, source_id: int) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    return source


async def _check_rule(session: AsyncSession, rule_id: int) -> None:
    if await session.get(Rule, rule_id) is None:
        raise HTTPException(status_code=422, detail=f"no rule {rule_id}")


@router.post("/sources", status_code=201)
async def create_sources(session: Session, body: SourceIn) -> list[SourceOut]:
    try:
        parsed = parse_source_url(body.url, body.tabs)
    except InvalidSourceUrl as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    await _check_rule(session, body.rule_id)
    urls = [p.url for p in parsed]
    taken = list(await session.scalars(select(Source.url).where(Source.url.in_(urls))))
    if taken:
        raise HTTPException(status_code=409, detail=f"already a source: {', '.join(taken)}")
    created = [
        Source(
            url=p.url,
            kind=p.kind,
            rule_id=body.rule_id,
            poll_interval_minutes=body.poll_interval_minutes,
            enabled=body.enabled,
        )
        for p in parsed
    ]
    session.add_all(created)
    await session.commit()
    return [SourceOut.model_validate(s) for s in created]


@router.patch("/sources/{source_id}")
async def update_source(session: Session, source_id: int, body: SourcePatch) -> SourceOut:
    source = await _source(session, source_id)
    if body.rule_id is not None:
        await _check_rule(session, body.rule_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(source, field, value)
    await session.commit()
    return SourceOut.model_validate(source)


@router.post("/sources/{source_id}/poll", status_code=202)
async def poll_now(session: Session, source_id: int) -> SourceOut:
    """The scheduler polls a never-polled source on its next tick."""
    source = await _source(session, source_id)
    source.last_polled_at = None
    await session.commit()
    return SourceOut.model_validate(source)


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(session: Session, source_id: int) -> None:
    """Stored videos and their files stay. Only the source's references go."""
    await session.delete(await _source(session, source_id))
    await session.commit()


@router.get("/rules")
async def rules(session: Session) -> list[RuleOut]:
    return [
        RuleOut.model_validate(r) for r in await session.scalars(select(Rule).order_by(Rule.id))
    ]


class RuleIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    types: Annotated[list[VideoType], Field(min_length=1)]
    date_after: date | None = None
    format: Annotated[str, Field(min_length=1)] = DEFAULT_FORMAT
    use_cookies: bool = False
    watch_lives: bool = False
    max_concurrent: Annotated[int, Field(ge=1, le=10)] = 1


async def _rule_name_free(session: AsyncSession, name: str, rule_id: int | None = None) -> None:
    q = select(Rule.id).where(Rule.name == name)
    if rule_id is not None:
        q = q.where(Rule.id != rule_id)
    if await session.scalar(q) is not None:
        raise HTTPException(status_code=409, detail=f"a rule named {name!r} exists")


@router.post("/rules", status_code=201)
async def create_rule(session: Session, body: RuleIn) -> RuleOut:
    await _rule_name_free(session, body.name)
    rule = Rule(**body.model_dump())
    session.add(rule)
    await session.commit()
    return RuleOut.model_validate(rule)


@router.put("/rules/{rule_id}")
async def replace_rule(session: Session, rule_id: int, body: RuleIn) -> RuleOut:
    rule = await session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404)
    await _rule_name_free(session, body.name, rule_id)
    for field, value in body.model_dump().items():
        setattr(rule, field, value)
    await session.commit()
    return RuleOut.model_validate(rule)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(session: Session, rule_id: int) -> None:
    rule = await session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404)
    used = await session.scalar(
        select(func.count()).select_from(Source).where(Source.rule_id == rule_id)
    )
    if used:
        raise HTTPException(status_code=409, detail=f"{used} sources use this rule")
    await session.delete(rule)
    await session.commit()


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
