"""Live channel listings and word previews for titles that are not stored."""

import asyncio
import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from yt_dlp.utils import DownloadError

from savetube import logs, words
from savetube.db.models import Source, SourceKind
from savetube.logs import emit
from savetube.sources import InvalidSourceUrl, Tab
from savetube.sources import parse as parse_source_url
from savetube.web.api.deps import Session, _channel_base, log
from savetube.web.api.schemas import WordCount, WordsIn, _Out
from savetube.ytdl import avatar_url, list_entries

router = APIRouter()


class ChannelPlaylist(_Out):
    id: str
    title: str
    url: str
    source_id: int | None


# A channel URL, and a cap on the entries listed or None for all of them.
Scanner = Callable[[str, int | None], dict[str, Any]]


def get_scanner() -> Scanner:
    """Listings without cookies, as the scheduler's poll makes them. Tests override it."""
    return lambda url, limit: list_entries(url, None, limit)


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
