import asyncio
import glob
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from yt_dlp.utils import DownloadError

from savetube import cookies, limits, logs, words
from savetube.db.models import (
    Channel,
    DownloadSettings,
    GlobalWords,
    Job,
    JobKind,
    JobState,
    ListingEntry,
    Membership,
    Source,
    SourceKind,
    StorageSample,
    Video,
    VideoSource,
    VideoState,
)
from savetube.jobs import BLOCKED_LOG, NETWORK_LOG, NO_MEMBER, SHUTDOWN_LOG, STALLED_LOG
from savetube.layout import VideoType
from savetube.limits import TIMEZONE
from savetube.logs import emit
from savetube.pipeline import get_or_create_channel
from savetube.sources import InvalidSourceUrl, Tab
from savetube.sources import parse as parse_source_url
from savetube.web.auth import CurrentUser, User, current_user
from savetube.ytdl import (
    DEFAULT_FORMAT,
    avatar_url,
    list_entries,
    members_only,
    network_error,
    youtube_block,
)

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


class MembershipOut(_Out):
    profile: str
    is_member: bool
    checked_at: datetime


class ChannelOut(_Out):
    id: str
    handle: str | None
    name: str
    dir_name: str
    include_words: list[str]
    exclude_words: list[str]
    date_after: date | None
    format: str
    watch_lives: bool
    avatar_url: str | None
    video_count: int
    bytes: int
    # False for a channel known only as the uploader of a video a source lists.
    tracked: bool
    # Each cookie profile's last membership check, by profile name.
    memberships: list[MembershipOut] = []


class SourceOut(_Out):
    id: int
    url: str
    title: str | None
    kind: str
    channel_id: str | None
    enabled: bool
    poll_interval_minutes: int
    include_words: list[str]
    exclude_words: list[str]
    other_channels: bool
    parent_id: int | None
    archiving: bool
    last_polled_at: datetime | None
    last_error: str | None


# yt-dlp's wrapping around the message itself: the exception name, its
# "ERROR:" prefix, a "[download] Got error:" retry line, and an
# "[extractor] <video id>:" tag.
_YTDLP_NOISE = re.compile(
    r"^(?:\w+Error:\s*)?(?:ERROR:\s*)?(?:\[download\]\s*Got error:\s*)?(?:\[\w+\]\s*[\w-]+:\s*)?"
)
_REASON_CHARS = 120


def job_reason(log_tail: str | None) -> str | None:
    """One plain line for a finished job's log_tail."""
    if not log_tail:
        return None
    text = log_tail.lower()
    if youtube_block(log_tail):
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
    # yt-dlp puts a carriage return before its retry line, so lines do not split it.
    flat = " ".join(log_tail.split())
    line = _YTDLP_NOISE.sub("", flat) or flat
    return line if len(line) <= _REASON_CHARS else line[: _REASON_CHARS - 3] + "..."


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
    # The integrity checks the stored download passed: size, probe, packets.
    verified_at: datetime | None
    verified_checks: list[str] | None


class VideoDetail(VideoOut):
    info: dict[str, Any] | None


class SubtitleOut(BaseModel):
    lang: str
    label: str


class VideoPage(_Out):
    total: int
    items: list[VideoOut]


# Every match runs each word against each listed title.
WordList = Annotated[list[Annotated[str, Field(max_length=100)]], Field(max_length=200)]


class WordsIn(BaseModel):
    include_words: WordList = []
    exclude_words: WordList = []


class ChannelPatch(BaseModel):
    include_words: WordList | None = None
    exclude_words: WordList | None = None
    # Null clears the cutoff, so only an omitted field leaves it alone.
    date_after: date | None = None
    format: Annotated[str | None, Field(min_length=1)] = None
    watch_lives: bool | None = None


@router.get("/me")
async def me(user: CurrentUser) -> User:
    return user


_size = func.coalesce(func.sum(Video.size_bytes), 0)
# Statistics count what sits in the tree, not pending or failed downloads.
_stored = Video.state == VideoState.COMPLETE
# A channel the user added: at least one source names it.
_tracked = select(Source.id).where(Source.channel_id == Channel.id).exists()


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
        # The channel list's rule: tracked channels only, and a video counts
        # when the channel uploaded it or one of its sources lists it.
        .join(Video, _of_channel(Channel.id) & _stored)
        .where(_tracked)
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
            Channel.date_after,
            Channel.format,
            Channel.watch_lives,
            Channel.avatar_url,
            func.count(Video.id).label("video_count"),
            _size.label("bytes"),
            _tracked.label("tracked"),
        )
        # The channel page's rule: uploaded by the channel or listed by its sources.
        .outerjoin(Video, _of_channel(Channel.id) & _stored)
        .group_by(Channel.id)
    )


async def _with_memberships(session: AsyncSession, rows) -> list[ChannelOut]:
    out = [ChannelOut.model_validate(r) for r in rows]
    by_channel = {c.id: c for c in out}
    for m in await session.scalars(
        select(Membership)
        .where(Membership.channel_id.in_(list(by_channel)))
        .order_by(Membership.profile)
    ):
        by_channel[m.channel_id].memberships.append(MembershipOut.model_validate(m))
    return out


@router.get("/channels")
async def channels(session: Session) -> list[ChannelOut]:
    rows = await session.execute(_channels_query().order_by(Channel.name))
    return await _with_memberships(session, rows)


@router.patch("/channels/{channel_id}")
async def update_channel(session: Session, channel_id: str, body: ChannelPatch) -> ChannelOut:
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404)
    _normalize(body)
    changed = {}
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None or field == "date_after":
            setattr(channel, field, value)
            changed[field] = value
    await session.commit()
    emit(
        log,
        logging.INFO,
        "channel.update",
        channel=channel_id,
        name=channel.name,
        **_logged(changed),
    )
    return await _channel_out(session, channel_id)


def _logged(fields: dict[str, Any]) -> dict[str, Any]:
    """Settings fields for a log line: a word list as its count."""
    out = {}
    for k, v in fields.items():
        if isinstance(v, list):
            out[k] = len(v)
        elif isinstance(v, bool):
            out[k] = "on" if v else "off"
        else:
            out[k] = v
    return out


async def _channel_out(session: AsyncSession, channel_id: str) -> ChannelOut:
    row = (await session.execute(_channels_query().where(Channel.id == channel_id))).one()
    [out] = await _with_memberships(session, [row])
    return out


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
    emit(
        log,
        logging.INFO,
        "words.update",
        scope="global",
        include=len(body.include_words),
        exclude=len(body.exclude_words),
    )
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
    poll_interval_minutes: Annotated[int, Field(ge=10, le=7 * 24 * 60)] = 360
    enabled: bool = True
    include_words: WordList = []
    exclude_words: WordList = []
    other_channels: bool = False
    # Off: polls list without downloading, so words can be set against the preview.
    archiving: bool = False


class SourcePatch(BaseModel):
    enabled: bool | None = None
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


# A channel URL, and a cap on the entries listed or None for all of them.
Scanner = Callable[[str, int | None], dict[str, Any]]


def get_scanner() -> Scanner:
    """Listings without cookies, as the scheduler's poll makes them. Tests override it."""
    return lambda url, limit: list_entries(url, None, limit)


async def _known_channel(session: AsyncSession, url: str) -> str | None:
    """The stored channel of a new tab or `playlists` source, when another source
    of the same channel URL already found it. Until then the first poll does."""
    if "/playlist?" in url:
        return None
    base = url.rsplit("/", 1)[0]
    return await session.scalar(
        select(Source.channel_id)
        .where(Source.url.startswith(f"{base}/", autoescape=True), Source.channel_id.is_not(None))
        .limit(1)
    )


async def _source(session: AsyncSession, source_id: int) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    return source


@router.post("/sources", status_code=201)
async def create_sources(session: Session, body: SourceIn) -> list[SourceOut]:
    try:
        parsed = parse_source_url(body.url, body.tabs)
    except InvalidSourceUrl as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    _normalize(body)
    urls = [p.url for p in parsed]
    taken = list(await session.scalars(select(Source.url).where(Source.url.in_(urls))))
    if taken:
        raise HTTPException(status_code=409, detail=f"already a source: {', '.join(taken)}")
    created = [
        Source(
            url=p.url,
            kind=p.kind,
            channel_id=await _known_channel(session, p.url),
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
    for s in created:
        emit(
            log,
            logging.INFO,
            "source.create",
            source=s.id,
            url=s.url,
            **_logged(body.model_dump(exclude={"url", "tabs"}, exclude_defaults=True)),
        )
    return [SourceOut.model_validate(s) for s in created]


@router.patch("/sources/{source_id}")
async def update_source(session: Session, source_id: int, body: SourcePatch) -> SourceOut:
    source = await _source(session, source_id)
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
    emit(log, logging.INFO, "source.update", source=source_id, url=source.url, **_logged(changes))
    return SourceOut.model_validate(source)


_INHERITED = frozenset({"poll_interval_minutes", "other_channels", "archiving"})


@router.post("/sources/{source_id}/poll", status_code=202)
async def poll_now(session: Session, source_id: int) -> SourceOut:
    """The scheduler polls a never-polled source on its next tick."""
    source = await _source(session, source_id)
    source.last_polled_at = None
    await session.commit()
    emit(log, logging.INFO, "source.poll_now", source=source_id, url=source.url)
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
    url = source.url
    await session.delete(source)
    await session.commit()
    emit(log, logging.INFO, "source.delete", source=source_id, url=url)


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
# Entries per tab in a channel scan: one page of YouTube's tab listing.
SCAN_PAGE = 30
# yt-dlp's answer for a tab the channel lacks, such as Shorts on a channel without any.
_NO_TAB = "does not have a"
SCAN_TABS = (Tab.VIDEOS, Tab.SHORTS, Tab.STREAMS)


def _channel_base(url: str) -> str:
    """`https://www.youtube.com/<channel>` for a channel URL, with or without a tab."""
    try:
        [parsed, *_] = parse_source_url(url, [Tab.VIDEOS])
    except InvalidSourceUrl as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if parsed.kind is SourceKind.PLAYLIST:
        raise HTTPException(status_code=422, detail="not a channel URL")
    return parsed.url.rsplit("/", 1)[0]


async def _list(scanner: Scanner, url: str, limit: int | None) -> dict[str, Any]:
    async with _listings:
        # On a timeout the thread runs on, and its result is dropped.
        return await asyncio.to_thread(scanner, url, limit)


def _playlists(listing: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Each playlist entry of a channel's /playlists listing, with its canonical URL."""
    found = []
    for e in listing.get("entries") or []:
        try:
            [p] = parse_source_url(e.get("url") or "")
        except InvalidSourceUrl:
            continue
        if p.kind is SourceKind.PLAYLIST:
            found.append((e, p.url))
    return found


@router.post("/channel-playlists")
async def channel_playlists(
    session: Session, body: ChannelUrl, scanner: Annotated[Scanner, Depends(get_scanner)]
) -> list[ChannelPlaylist]:
    """The channel's playlists, listed live, each with its source if one exists."""
    url = f"{_channel_base(body.url)}/{Tab.PLAYLISTS}"
    try:
        async with asyncio.timeout(CHANNEL_PLAYLISTS_TIMEOUT):
            listing = await _list(scanner, url, None)
    except TimeoutError:
        _scan_failed(url, 504, "timed out")
        raise HTTPException(status_code=504, detail="listing the playlists timed out") from None
    except DownloadError as e:
        _scan_failed(url, 502, str(e))
        raise HTTPException(status_code=502, detail=str(e)) from None
    playlists = _playlists(listing)
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


def _scan_failed(url: str, status: int, reason: str) -> None:
    emit(log, logging.WARNING, "scan.failed", url=url, status=status, reason=logs.cut(reason))


class ScanEntry(_Out):
    video_id: str
    title: str


class ScanPlaylist(_Out):
    id: str
    title: str
    url: str


class ChannelScan(_Out):
    url: str
    channel_id: str
    handle: str | None
    name: str
    avatar_url: str | None
    # Keyed by tab. Null for a tab the channel lacks.
    tabs: dict[str, list[ScanEntry] | None]
    playlists: list[ScanPlaylist]
    # Words that recur in the scanned titles, best first.
    suggestions: list[WordCount]


async def _scan_one(scanner: Scanner, url: str, limit: int | None) -> dict[str, Any] | None:
    try:
        return await _list(scanner, url, limit)
    except DownloadError as e:
        if _NO_TAB in str(e):
            return None
        raise


@router.post("/channel-scan")
async def channel_scan(
    body: ChannelUrl, scanner: Annotated[Scanner, Depends(get_scanner)]
) -> ChannelScan:
    """The first page of each video tab and every playlist, listed live, so the
    words can be set against real titles before anything is stored."""
    base = _channel_base(body.url)
    try:
        async with asyncio.timeout(CHANNEL_PLAYLISTS_TIMEOUT):
            *tabs, playlists = await asyncio.gather(
                *(_scan_one(scanner, f"{base}/{t}", SCAN_PAGE) for t in SCAN_TABS),
                _scan_one(scanner, f"{base}/{Tab.PLAYLISTS}", None),
            )
    except TimeoutError:
        _scan_failed(base, 504, "timed out")
        raise HTTPException(status_code=504, detail="scanning the channel timed out") from None
    except DownloadError as e:
        _scan_failed(base, 502, str(e))
        raise HTTPException(status_code=502, detail=str(e)) from None
    owner = next((x for x in (*tabs, playlists) if x and x.get("channel_id")), None)
    if owner is None:
        raise HTTPException(status_code=502, detail="the channel lists no tab")
    entries = {
        t.value: None
        if listing is None
        else [
            ScanEntry(video_id=e["id"], title=e.get("title") or e["id"])
            for e in listing.get("entries") or []
            if e.get("id")
        ]
        for t, listing in zip(SCAN_TABS, tabs, strict=True)
    }
    titles = [e.title for listed in entries.values() for e in listed or []]
    return ChannelScan(
        url=base,
        channel_id=owner["channel_id"],
        handle=owner.get("uploader_id"),
        name=owner.get("channel") or owner.get("uploader") or owner["channel_id"],
        avatar_url=next((a for x in tabs if x and (a := avatar_url(x))), None),
        tabs=entries,
        playlists=[
            ScanPlaylist(id=e["id"], title=e.get("title") or e["id"], url=u)
            for e, u in _playlists(playlists or {})
        ],
        suggestions=[WordCount(word=w, count=n) for w, n in words.rank(titles)],
    )


class TitlePreviewIn(WordsIn):
    channel_id: str | None = None
    titles: Annotated[list[str], Field(max_length=1000)]


class TitleKept(_Out):
    title: str
    kept: bool


@router.post("/title-preview")
async def title_preview_any(session: Session, body: TitlePreviewIn) -> list[TitleKept]:
    """How the words would filter titles that are not stored, such as a scan's.
    The stored global lists, and the channel's when it is stored, are merged in."""
    lists = await words.effective(
        session, body.channel_id, (body.include_words, body.exclude_words)
    )
    return [TitleKept(title=t, kept=words.matches(t, *lists)) for t in body.titles]


class PlaylistPick(BaseModel):
    url: str
    title: str | None = None


class ChannelIn(WordsIn):
    # The scan's `url`, `channel_id`, `handle` and `name`.
    url: str
    channel_id: Annotated[str, Field(min_length=1, max_length=64)]
    handle: str | None = None
    name: Annotated[str, Field(min_length=1)]
    avatar_url: str | None = None
    tabs: list[Literal[Tab.VIDEOS, Tab.SHORTS, Tab.STREAMS]] = []
    # Track all: one `playlists` source. Otherwise one source per picked playlist.
    all_playlists: bool = False
    playlists: list[PlaylistPick] = []
    # Applies to the playlist sources only. A tab keeps what its channel tab lists.
    other_channels: bool = True
    watch_lives: bool = False
    date_after: date | None = None
    format: Annotated[str, Field(min_length=1)] = DEFAULT_FORMAT
    poll_interval_minutes: Annotated[int, Field(ge=10, le=7 * 24 * 60)] = 360


class ChannelWithSources(ChannelOut):
    sources: list[SourceOut]


@router.post("/channels", status_code=201)
async def create_channel(session: Session, body: ChannelIn) -> ChannelWithSources:
    """The channel with its settings and its sources, archiving from the next tick."""
    if not body.tabs and not body.all_playlists and not body.playlists:
        raise HTTPException(status_code=422, detail="choose a tab or a playlist")
    if body.watch_lives and not body.tabs:
        raise HTTPException(status_code=422, detail="recording lives needs a tab")
    base = _channel_base(body.url)
    _normalize(body)
    wanted: list[tuple[str, SourceKind, str | None]] = [
        (f"{base}/{t}", SourceKind.TAB, None) for t in dict.fromkeys(body.tabs)
    ]
    if body.all_playlists:
        wanted.append((f"{base}/{Tab.PLAYLISTS}", SourceKind.PLAYLISTS, None))
    else:
        for pick in body.playlists:
            try:
                [p] = parse_source_url(pick.url)
            except InvalidSourceUrl as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
            if p.kind is not SourceKind.PLAYLIST:
                raise HTTPException(status_code=422, detail=f"not a playlist: {pick.url}")
            wanted.append((p.url, SourceKind.PLAYLIST, pick.title))
    urls = [u for u, _, _ in wanted]
    taken = list(await session.scalars(select(Source.url).where(Source.url.in_(urls))))
    if taken:
        raise HTTPException(status_code=409, detail=f"already a source: {', '.join(taken)}")

    # A channel stored as another playlist's uploader keeps its directory name.
    channel = await get_or_create_channel(
        session, {"channel_id": body.channel_id, "channel": body.name, "uploader_id": body.handle}
    )
    channel.include_words, channel.exclude_words = body.include_words, body.exclude_words
    channel.date_after, channel.format = body.date_after, body.format
    channel.watch_lives = body.watch_lives
    channel.avatar_url = body.avatar_url or channel.avatar_url
    # No relationship() orders inserts, so the FK target is flushed first.
    await session.flush()
    created = [
        Source(
            url=url,
            kind=kind,
            title=title,
            channel_id=channel.id,
            poll_interval_minutes=body.poll_interval_minutes,
            other_channels=body.other_channels and kind is not SourceKind.TAB,
            archiving=True,
        )
        for url, kind, title in wanted
    ]
    session.add_all(created)
    await session.commit()
    emit(
        log,
        logging.INFO,
        "channel.create",
        channel=channel.id,
        name=channel.name,
        sources=",".join(str(s.id) for s in created),
        archiving="on",
    )
    out = await _channel_out(session, body.channel_id)
    return ChannelWithSources(
        **out.model_dump(), sources=[SourceOut.model_validate(s) for s in created]
    )


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


def _of_channel(channel_id: str | ColumnElement[str]) -> ColumnElement[bool]:
    """Videos the channel uploaded or one of its sources lists. A collaboration
    on the channel's tab is stored under the uploader the video's info names."""
    listed = (
        select(VideoSource.video_id)
        .join(Source, Source.id == VideoSource.source_id)
        .where(Source.channel_id == channel_id)
    )
    return (Video.channel_id == channel_id) | Video.id.in_(listed)


@router.get("/jobs")
async def jobs(
    session: Session,
    kind: JobKind | None = None,
    # Repeat the parameter for several states.
    state: Annotated[list[JobState] | None, Query()] = None,
    channel_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[JobOut]:
    q = (
        select(Job, Video.title, Video.channel_id, Channel.name)
        .outerjoin(Video, Video.id == Job.video_id)
        .outerjoin(Channel, Channel.id == Video.channel_id)
        .order_by(Job.id.desc())
        .limit(limit)
    )
    if kind:
        q = q.where(Job.kind == kind)
    if state:
        q = q.where(Job.state.in_(state))
    if channel_id:
        q = q.where(_of_channel(channel_id))
    own = JobOut.model_fields.keys() - {"title", "channel_id", "channel_name", "reason"}
    return [
        JobOut(
            **{f: getattr(j, f) for f in own},
            title=title,
            channel_id=cid,
            channel_name=name,
            reason=job_reason(j.log_tail),
        )
        for j, title, cid, name in await session.execute(q)
    ]


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


class StagedOut(BaseModel):
    count: int
    bytes: int


@router.get("/staging")
async def staging(session: Session) -> StagedOut:
    """The videos waiting on staging for the move into the tree."""
    count, size = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Video.size_bytes), 0)).where(
                Video.state == VideoState.STAGED
            )
        )
    ).one()
    return StagedOut(count=count, bytes=size)


@router.get("/videos")
async def videos(
    session: Session,
    channel_id: str | None = None,
    # The channel page's view: also the videos the channel's sources list.
    listed_by: str | None = None,
    # Videos the source lists. A `playlists` parent lists through its children.
    source_id: int | None = None,
    type: VideoType | None = None,
    state: VideoState | None = None,
    q: str | None = None,
    # By upload date. A video without one sorts last either way.
    order: Literal["newest", "oldest"] = "newest",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> VideoPage:
    where = []
    if channel_id:
        where.append(Video.channel_id == channel_id)
    if listed_by:
        where.append(_of_channel(listed_by))
    if source_id is not None:
        sources = select(Source.id).where(
            (Source.id == source_id) | (Source.parent_id == source_id)
        )
        listed = select(VideoSource.video_id).where(VideoSource.source_id.in_(sources))
        where.append(Video.id.in_(listed))
    if type:
        where.append(Video.type == type)
    if state:
        where.append(Video.state == state)
    if q:
        pattern = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
        where.append(or_(Video.title.ilike(pattern), Video.id == q))
    total = await session.scalar(select(func.count()).select_from(Video).where(*where))
    by_date = Video.upload_date.desc() if order == "newest" else Video.upload_date.asc()
    items = await session.scalars(
        select(Video)
        .where(*where)
        .order_by(by_date.nulls_last(), Video.id)
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


async def _video_dir(request: Request, session: AsyncSession, video_id: str) -> tuple[Video, Path]:
    """The resolved `<archive root>/<dir_path>` of a complete video, built from
    its row only and never outside the archive root."""
    v = await session.get(Video, video_id)
    if v is None or v.state is not VideoState.COMPLETE or not v.dir_path:
        raise HTTPException(status_code=404)
    root = request.app.state.archive_root.resolve()
    directory = (root / v.dir_path).resolve()
    if not directory.is_relative_to(root) or not directory.is_dir():
        raise HTTPException(status_code=404)
    return v, directory


async def _stored_file(request: Request, session: AsyncSession, video_id: str, ext: str) -> Path:
    """`<video dir>/<video id>.<ext>`. `ext` "" means the media's own extension
    from `info.ext`."""
    v, directory = await _video_dir(request, session, video_id)
    ext = ext or (v.info or {}).get("ext")
    if not isinstance(ext, str) or not ext:
        raise HTTPException(status_code=404)
    path = (directory / f"{v.id}.{ext}").resolve()
    if not path.is_relative_to(request.app.state.archive_root.resolve()) or not path.is_file():
        raise HTTPException(status_code=404)
    return path


async def _subtitle_files(
    request: Request, session: AsyncSession, video_id: str
) -> tuple[Video, dict[str, Path]]:
    """Language code to file for each `<video id>.<lang>.vtt` yt-dlp left in the
    video's directory. The language comes from the listing, never from a request."""
    v, directory = await _video_dir(request, session, video_id)
    files = {}
    for path in directory.glob(f"{glob.escape(v.id)}.*.vtt"):
        lang = path.name[len(v.id) + 1 : -len(".vtt")]
        resolved = path.resolve()
        if (
            lang
            and lang != "live_chat"
            and resolved.is_relative_to(directory)
            and resolved.is_file()
        ):
            files[lang] = resolved
    return v, dict(sorted(files.items()))


def _subtitle_label(info: dict[str, Any] | None, lang: str) -> str:
    tracks = ((info or {}).get("subtitles") or {}).get(lang)
    if isinstance(tracks, list):
        for track in tracks:
            if isinstance(track, dict) and isinstance(track.get("name"), str) and track["name"]:
                return track["name"]
    return lang


@router.get("/videos/{video_id}/subtitles")
async def video_subtitles(request: Request, session: Session, video_id: str) -> list[SubtitleOut]:
    v, files = await _subtitle_files(request, session, video_id)
    return [SubtitleOut(lang=lang, label=_subtitle_label(v.info, lang)) for lang in files]


@router.get("/videos/{video_id}/subtitles/{lang}")
async def video_subtitle(
    request: Request, session: Session, video_id: str, lang: str
) -> FileResponse:
    _, files = await _subtitle_files(request, session, video_id)
    if lang not in files:
        raise HTTPException(status_code=404)
    return FileResponse(files[lang], media_type="text/vtt; charset=utf-8")


# Starlette's FileResponse answers Range (206, 416, multipart) and HEAD itself.
@router.api_route("/videos/{video_id}/media", methods=["GET", "HEAD"])
async def video_media(request: Request, session: Session, video_id: str) -> FileResponse:
    path = await _stored_file(request, session, video_id, "")
    return FileResponse(path, media_type="video/x-matroska" if path.suffix == ".mkv" else None)


@router.api_route("/videos/{video_id}/audio", methods=["GET", "HEAD"])
async def video_audio(request: Request, session: Session, video_id: str) -> FileResponse:
    return FileResponse(
        await _stored_file(request, session, video_id, "opus"), media_type="audio/ogg"
    )


_HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class DownloadSettingsIn(BaseModel):
    max_concurrent: Annotated[int, Field(ge=1, le=10)] = 1
    rate_limit_bytes: Annotated[int | None, Field(gt=0)] = None
    window_start: _HHMM | None = None
    window_end: _HHMM | None = None
    # Off keeps the times and ignores them.
    window_enabled: bool = False
    # Archive downloads per Asia/Seoul day. Null for no cap.
    daily_limit: Annotated[int | None, Field(ge=1)] = None
    # A present cookie profile's name. Null runs polls, live probes and
    # non-members downloads cookie-free.
    cookie_profile: str | None = None

    @model_validator(mode="after")
    def _window(self) -> DownloadSettingsIn:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("set both window_start and window_end, or neither")
        if self.window_start is not None and self.window_start == self.window_end:
            raise ValueError("window_start and window_end must differ")
        if self.window_enabled and self.window_start is None:
            raise ValueError("set window_start and window_end to enable the window")
        return self


class DownloadSettingsOut(DownloadSettingsIn):
    timezone: str
    # Set by a YouTube block, read-only here (savetube.jobs).
    blocked_until: datetime | None = None
    block_count: int = 0
    # Set through PUT /api/download-settings/paused only.
    paused: bool = False
    # Archive downloads succeeded today in Asia/Seoul, plus running ones.
    downloaded_today: int = 0


class PausedIn(BaseModel):
    paused: bool


def _hhmm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t is not None else None


async def _settings_out(session: AsyncSession, row: DownloadSettings) -> DownloadSettingsOut:
    return DownloadSettingsOut(
        max_concurrent=row.max_concurrent,
        rate_limit_bytes=row.rate_limit_bytes,
        window_start=_hhmm(row.window_start),
        window_end=_hhmm(row.window_end),
        window_enabled=row.window_enabled,
        daily_limit=row.daily_limit,
        cookie_profile=row.cookie_profile,
        timezone=TIMEZONE.key,
        blocked_until=row.blocked_until,
        block_count=row.block_count,
        paused=row.paused,
        downloaded_today=await limits.downloaded_today(session, datetime.now(UTC)),
    )


@router.get("/download-settings")
async def download_settings(session: Session) -> DownloadSettingsOut:
    return await _settings_out(session, await limits.load(session))


@router.put("/download-settings")
async def replace_download_settings(
    session: Session, body: DownloadSettingsIn
) -> DownloadSettingsOut:
    """The scheduler's next dispatch applies the new limits to waiting jobs."""
    present = await cookies.recorded_profiles(session)
    if body.cookie_profile is not None and body.cookie_profile not in present:
        raise HTTPException(
            status_code=422, detail=f"no cookie profile named {body.cookie_profile}"
        )
    row = await session.get(DownloadSettings, 1)
    if row is None:
        row = DownloadSettings(id=1)
        session.add(row)
    row.max_concurrent = body.max_concurrent
    row.rate_limit_bytes = body.rate_limit_bytes
    row.window_start = time.fromisoformat(body.window_start) if body.window_start else None
    row.window_end = time.fromisoformat(body.window_end) if body.window_end else None
    row.window_enabled = body.window_enabled
    row.daily_limit = body.daily_limit
    row.cookie_profile = body.cookie_profile
    await session.commit()
    emit(
        log,
        logging.INFO,
        "settings.download",
        max_concurrent=body.max_concurrent,
        rate_limit=f"{logs.human_bytes(body.rate_limit_bytes)}/s"
        if body.rate_limit_bytes
        else "none",
        window=limits.window_label(row) or "none",
        daily_limit=body.daily_limit or "none",
        cookie_profile=body.cookie_profile or "none",
    )
    return await _settings_out(session, row)


@router.put("/download-settings/paused")
async def pause_downloads(session: Session, body: PausedIn) -> DownloadSettingsOut:
    """Pause or resume archive downloads. The next dispatch, and every running
    download within PAUSE_POLL_SECONDS, applies it. Recordings ignore it."""
    row = await session.get(DownloadSettings, 1)
    if row is None:
        row = DownloadSettings(id=1)
        session.add(row)
    changed = row.paused != body.paused
    row.paused = body.paused
    running = await limits.running(session)
    await session.commit()
    if changed:
        emit(
            log,
            logging.INFO,
            "dispatch.paused" if body.paused else "dispatch.resumed",
            running=running,
        )
    return await _settings_out(session, row)


class CookieProfileOut(_Out):
    name: str


@router.get("/cookie-profiles")
async def cookie_profiles(session: Session) -> list[CookieProfileOut]:
    """The profiles the scheduler last found in SAVETUBE_COOKIES_DIR, by name.
    The web pod does not mount them."""
    return [CookieProfileOut(name=n) for n in await cookies.recorded_profiles(session)]
