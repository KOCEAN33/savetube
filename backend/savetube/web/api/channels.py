import logging
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube import words
from savetube.db.models import Channel, ListingEntry, Membership, Source, SourceKind, Video
from savetube.logs import emit
from savetube.pipeline import get_or_create_channel
from savetube.sources import InvalidSourceUrl, Tab
from savetube.sources import parse as parse_source_url
from savetube.web.api.deps import (
    Session,
    _channel_base,
    _logged,
    _normalize,
    _of_channel,
    _size,
    _stored,
    _tracked,
    log,
)
from savetube.web.api.schemas import SourceOut, WordCount, WordList, WordsIn, _Out
from savetube.ytdl import DEFAULT_FORMAT

router = APIRouter()


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
    members_all: bool
    avatar_url: str | None
    video_count: int
    bytes: int
    # False for a channel known only as the uploader of a video a source lists.
    tracked: bool
    # Each cookie profile's last membership check, by profile name.
    memberships: list[MembershipOut] = []


class ChannelPatch(BaseModel):
    include_words: WordList | None = None
    exclude_words: WordList | None = None
    # Null clears the cutoff, so only an omitted field leaves it alone.
    date_after: date | None = None
    format: Annotated[str | None, Field(min_length=1)] = None
    watch_lives: bool | None = None
    members_all: bool | None = None


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
            Channel.members_all,
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


async def _channel_out(session: AsyncSession, channel_id: str) -> ChannelOut:
    row = (await session.execute(_channels_query().where(Channel.id == channel_id))).one()
    [out] = await _with_memberships(session, [row])
    return out


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
