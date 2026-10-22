import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from savetube import words
from savetube.db.models import Channel, ListingEntry, Source, SourceKind
from savetube.logs import emit
from savetube.sources import InvalidSourceUrl, Tab
from savetube.sources import parse as parse_source_url
from savetube.web.api.deps import Session, _logged, _normalize, log
from savetube.web.api.schemas import SourceOut, WordList, WordsIn, _Out

router = APIRouter()


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
    merged in, as the poll does, and the include words drop for a playlist of
    a channel with `playlists_pass` on."""
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
    if source.kind is SourceKind.PLAYLIST and source.channel_id is not None:
        channel = await session.get(Channel, source.channel_id)
        if channel is not None and channel.playlists_pass:
            lists = {cid: ([], exclude) for cid, (_, exclude) in lists.items()}
    titles = [
        PreviewTitle(
            video_id=r.video_id, title=r.title, kept=words.matches(r.title, *lists[r.channel_id])
        )
        for r in rows
    ]
    return Preview(total=len(titles), kept=sum(t.kept for t in titles), titles=titles)
