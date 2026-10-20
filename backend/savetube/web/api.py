import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from yt_dlp.utils import DownloadError

from savetube import limits, words
from savetube.db.models import (
    Channel,
    DownloadSettings,
    GlobalWords,
    Job,
    JobKind,
    JobState,
    ListingEntry,
    Rule,
    Source,
    SourceKind,
    StorageSample,
    Video,
    VideoState,
)
from savetube.layout import VideoType
from savetube.limits import TIMEZONE
from savetube.poll import Lister
from savetube.sources import InvalidSourceUrl, Tab
from savetube.sources import parse as parse_source_url
from savetube.web.auth import CurrentUser, User, current_user
from savetube.ytdl import DEFAULT_FORMAT, list_entries

log = logging.getLogger(__name__)


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
    include_words: list[str]
    exclude_words: list[str]
    video_count: int
    bytes: int


class SourceOut(_Out):
    id: int
    url: str
    title: str | None
    kind: str
    channel_id: str | None
    enabled: bool
    poll_interval_minutes: int
    rule_id: int
    include_words: list[str]
    exclude_words: list[str]
    other_channels: bool
    parent_id: int | None
    archiving: bool
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


class JobOut(_Out):
    id: int
    kind: JobKind
    state: JobState
    video_id: str | None
    attempts: int
    miss_reason: str | None
    scheduled_at: datetime | None
    deferred_until: datetime | None
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


# Every match runs each word against each listed title.
WordList = Annotated[list[Annotated[str, Field(max_length=100)]], Field(max_length=200)]


class WordsIn(BaseModel):
    include_words: WordList = []
    exclude_words: WordList = []


class WordsPatch(BaseModel):
    include_words: WordList | None = None
    exclude_words: WordList | None = None


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


def _channels_query():
    return (
        select(
            Channel.id,
            Channel.handle,
            Channel.name,
            Channel.dir_name,
            Channel.include_words,
            Channel.exclude_words,
            func.count(Video.id).label("video_count"),
            _size.label("bytes"),
        )
        .outerjoin(Video, (Video.channel_id == Channel.id) & _stored)
        .group_by(Channel.id)
    )


@router.get("/channels")
async def channels(session: Session) -> list[ChannelOut]:
    rows = await session.execute(_channels_query().order_by(Channel.name))
    return [ChannelOut.model_validate(r) for r in rows]


@router.patch("/channels/{channel_id}")
async def update_channel(session: Session, channel_id: str, body: WordsPatch) -> ChannelOut:
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404)
    _normalize(body)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(channel, field, value)
    await session.commit()
    row = (await session.execute(_channels_query().where(Channel.id == channel_id))).one()
    return ChannelOut.model_validate(row)


@router.get("/words")
async def global_words(session: Session) -> WordsIn:
    row = await session.get(GlobalWords, 1)
    if row is None:
        return WordsIn()
    return WordsIn(include_words=row.include_words, exclude_words=row.exclude_words)


@router.put("/words")
async def replace_global_words(session: Session, body: WordsIn) -> WordsIn:
    _normalize(body)
    row = await session.get(GlobalWords, 1)
    if row is None:
        row = GlobalWords(id=1)
        session.add(row)
    row.include_words, row.exclude_words = body.include_words, body.exclude_words
    await session.commit()
    return body


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
    include_words: WordList = []
    exclude_words: WordList = []
    other_channels: bool = False
    # Off: polls list without downloading, so words can be set against the preview.
    archiving: bool = False


class SourcePatch(BaseModel):
    enabled: bool | None = None
    rule_id: int | None = None
    poll_interval_minutes: Annotated[int | None, Field(ge=10, le=7 * 24 * 60)] = None
    include_words: WordList | None = None
    exclude_words: WordList | None = None
    other_channels: bool | None = None
    archiving: bool | None = None


class PreviewTitle(_Out):
    video_id: str
    title: str
    kept: bool


class Preview(_Out):
    total: int
    kept: int
    titles: list[PreviewTitle]


class ChannelPlaylist(_Out):
    id: str
    title: str
    url: str
    source_id: int | None


class WordCount(_Out):
    word: str
    count: int


def _normalize(body: BaseModel) -> None:
    for field in ("include_words", "exclude_words"):
        value = getattr(body, field)
        if value is not None:
            setattr(body, field, words.normalize_words(value))


def get_lister() -> Lister:
    """The lister the scheduler's poll uses. Tests override it."""
    return list_entries


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
    _normalize(body)
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
            include_words=body.include_words,
            exclude_words=body.exclude_words,
            other_channels=body.other_channels,
            archiving=body.archiving,
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
    _normalize(body)
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(source, field, value)
    if body.archiving:
        # Polled on the next scheduler tick.
        source.last_polled_at = None
    # Words are not copied: a child merges its parent's lists when it is matched.
    inherited = {k: v for k, v in changes.items() if k in _INHERITED}
    if inherited:
        await session.execute(
            update(Source).where(Source.parent_id == source_id).values(**inherited)
        )
    await session.commit()
    return SourceOut.model_validate(source)


_INHERITED = frozenset({"rule_id", "poll_interval_minutes", "other_channels", "archiving"})


@router.post("/sources/{source_id}/poll", status_code=202)
async def poll_now(session: Session, source_id: int) -> SourceOut:
    """The scheduler polls a never-polled source on its next tick."""
    source = await _source(session, source_id)
    source.last_polled_at = None
    await session.commit()
    return SourceOut.model_validate(source)


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(session: Session, source_id: int) -> None:
    """Stored videos and their files stay. Only the source's references go, and
    the playlist sources a `playlists` source created. Such a child is only
    disabled: its row keeps the parent poll from creating it again."""
    source = await _source(session, source_id)
    if source.parent_id is not None:
        raise HTTPException(
            status_code=409, detail="a playlist of a tracked channel can only be disabled"
        )
    await session.delete(source)
    await session.commit()


@router.post("/sources/{source_id}/title-preview")
async def title_preview(session: Session, source_id: int, body: WordsIn) -> Preview:
    """How the words would filter the source's last listing. The body holds the
    source's own lists. The stored global, uploader channel and parent lists are
    merged in, as the poll does."""
    source = await _source(session, source_id)
    own = [(body.include_words, body.exclude_words)]
    if source.parent_id is not None:
        parent = await session.get(Source, source.parent_id)
        own.insert(0, (parent.include_words, parent.exclude_words))
    rows = (
        await session.execute(
            select(ListingEntry.video_id, ListingEntry.title, ListingEntry.channel_id)
            .where(ListingEntry.source_id == source_id)
            .order_by(ListingEntry.title, ListingEntry.video_id)
        )
    ).all()
    lists = {cid: await words.effective(session, cid, *own) for cid in {r.channel_id for r in rows}}
    titles = [
        PreviewTitle(
            video_id=r.video_id, title=r.title, kept=words.matches(r.title, *lists[r.channel_id])
        )
        for r in rows
    ]
    return Preview(total=len(titles), kept=sum(t.kept for t in titles), titles=titles)


class ChannelUrl(BaseModel):
    url: str


# A listing holds a yt-dlp extraction and YouTube requests for its whole run.
_listings = asyncio.Semaphore(2)
CHANNEL_PLAYLISTS_TIMEOUT = 120


@router.post("/channel-playlists")
async def channel_playlists(
    session: Session, body: ChannelUrl, lister: Annotated[Lister, Depends(get_lister)]
) -> list[ChannelPlaylist]:
    """The channel's playlists, listed live, each with its source if one exists."""
    try:
        [parsed] = parse_source_url(body.url, [Tab.PLAYLISTS])
    except InvalidSourceUrl as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if parsed.kind is not SourceKind.PLAYLISTS:
        raise HTTPException(status_code=422, detail="not a channel URL")
    try:
        async with _listings, asyncio.timeout(CHANNEL_PLAYLISTS_TIMEOUT):
            # On a timeout the thread runs on, and its result is dropped.
            listing = await asyncio.to_thread(lister, parsed.url, None)
    except TimeoutError:
        raise HTTPException(status_code=504, detail="listing the playlists timed out") from None
    except DownloadError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None
    playlists = []
    for e in listing.get("entries") or []:
        try:
            [p] = parse_source_url(e.get("url") or "")
        except InvalidSourceUrl:
            continue
        if p.kind is SourceKind.PLAYLIST:
            playlists.append((e, p.url))
    sources = dict(
        (
            await session.execute(
                select(Source.url, Source.id).where(Source.url.in_([u for _, u in playlists]))
            )
        ).all()
    )
    return [
        ChannelPlaylist(
            id=e["id"], title=e.get("title") or e["id"], url=u, source_id=sources.get(u)
        )
        for e, u in playlists
    ]


@router.get("/channels/{channel_id}/words")
async def channel_words(session: Session, channel_id: str) -> list[WordCount]:
    """Title word suggestions from every listing that showed the channel's videos."""
    if await session.get(Channel, channel_id) is None:
        raise HTTPException(status_code=404)
    titles = await session.scalars(
        select(func.min(ListingEntry.title))
        .where(ListingEntry.channel_id == channel_id)
        .group_by(ListingEntry.video_id)
    )
    return [WordCount(word=w, count=n) for w, n in words.rank(list(titles))]


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


_HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class DownloadSettingsIn(BaseModel):
    max_concurrent: Annotated[int, Field(ge=1, le=10)] = 1
    rate_limit_bytes: Annotated[int | None, Field(gt=0)] = None
    window_start: _HHMM | None = None
    window_end: _HHMM | None = None

    @model_validator(mode="after")
    def _window(self) -> DownloadSettingsIn:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("set both window_start and window_end, or neither")
        if self.window_start is not None and self.window_start == self.window_end:
            raise ValueError("window_start and window_end must differ")
        return self


class DownloadSettingsOut(DownloadSettingsIn):
    timezone: str


def _hhmm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t is not None else None


def _settings_out(row: DownloadSettings) -> DownloadSettingsOut:
    return DownloadSettingsOut(
        max_concurrent=row.max_concurrent,
        rate_limit_bytes=row.rate_limit_bytes,
        window_start=_hhmm(row.window_start),
        window_end=_hhmm(row.window_end),
        timezone=TIMEZONE.key,
    )


@router.get("/download-settings")
async def download_settings(session: Session) -> DownloadSettingsOut:
    return _settings_out(await limits.load(session))


# Replaces a deferred job's queue job with one due now. None when a worker
# took the old one first.
Requeue = Callable[[int, int | None], Awaitable[int | None]]


async def get_requeue() -> AsyncIterator[Requeue]:
    """The queue, opened for this request only. Tests override it."""
    from savetube.queue import app, requeue_now

    async with app.open_async():
        yield requeue_now


@router.put("/download-settings")
async def replace_download_settings(
    session: Session, body: DownloadSettingsIn, requeue: Annotated[Requeue, Depends(get_requeue)]
) -> DownloadSettingsOut:
    """Deferred jobs run again at once, so a widened or removed window or a
    raised limit takes effect now instead of at the old deferral time."""
    row = await session.get(DownloadSettings, 1)
    if row is None:
        row = DownloadSettings(id=1)
        session.add(row)
    row.max_concurrent = body.max_concurrent
    row.rate_limit_bytes = body.rate_limit_bytes
    row.window_start = time.fromisoformat(body.window_start) if body.window_start else None
    row.window_end = time.fromisoformat(body.window_end) if body.window_end else None
    await session.commit()
    deferred = await session.scalars(
        select(Job).where(
            Job.kind == JobKind.ARCHIVE,
            Job.state == JobState.QUEUED,
            Job.deferred_until.is_not(None),
        )
    )
    for job in deferred.all():
        try:
            queue_job_id = await requeue(job.id, job.queue_job_id)
        except Exception:
            log.exception("requeue of job %s failed", job.id)
            continue
        if queue_job_id is not None:
            job.queue_job_id, job.deferred_until = queue_job_id, None
            await session.commit()
    return _settings_out(row)
