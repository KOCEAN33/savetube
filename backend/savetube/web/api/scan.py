"""Live channel listings and word previews for titles that are not stored."""

import asyncio
import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from yt_dlp.utils import DownloadError

from savetube import logs, words
from savetube.db.models import Source
from savetube.logs import emit
from savetube.scan import playlists as _playlists
from savetube.sources import Tab
from savetube.web.api.deps import Session, _channel_base, call_scheduler, log
from savetube.web.api.schemas import WordCount, WordsIn, _Out
from savetube.ytdl import list_entries

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
# The scheduler answers within 110 s (savetube.scan.SCAN_SECONDS).
CHANNEL_SCAN_TIMEOUT = 120


async def _list(scanner: Scanner, url: str, limit: int | None) -> dict[str, Any]:
    async with _listings:
        # On a timeout the thread runs on, and its result is dropped.
        return await asyncio.to_thread(scanner, url, limit)


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


class ScanMembership(_Out):
    profile: str
    # Members-only entries the profile's listing showed, null when it failed.
    members_only: int | None
    # The stored membership result, null without one.
    is_member: bool | None


class ChannelScan(_Out):
    url: str
    channel_id: str
    handle: str | None
    name: str
    avatar_url: str | None
    # Keyed by tab. Null for a tab the channel lacks.
    tabs: dict[str, list[ScanEntry] | None]
    playlists: list[ScanPlaylist]
    # One per cookie profile the scan listed with, by name.
    memberships: list[ScanMembership]
    # Words that recur in the scanned titles, best first.
    suggestions: list[WordCount]


@router.post("/channel-scan")
async def channel_scan(request: Request, body: ChannelUrl) -> ChannelScan:
    """The first page of each video tab and every playlist, listed live by the
    scheduler with the cookie profiles, so the words can be set against real
    titles before the sources are stored."""
    base = _channel_base(body.url)

    def failed(status: int, reason: str) -> None:
        emit(log, logging.WARNING, "scan.request_failed", url=base, status=status, reason=reason)

    r = await call_scheduler(
        request,
        "/channel-scan",
        seconds=CHANNEL_SCAN_TIMEOUT,
        failed=failed,
        json={"url": base},
        passes=(404, 409, 502, 504),
    )
    scan = r.json()
    titles = [e["title"] for listed in scan["tabs"].values() for e in listed or []]
    return ChannelScan(
        **scan, suggestions=[WordCount(word=w, count=n) for w, n in words.rank(titles)]
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
