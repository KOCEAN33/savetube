"""The add-channel scan (docs/plan/frontend.md, Adding a channel), run by the
scheduler because it lists with the cookie profiles, which the web pod never
mounts (docs/plan/access.md, Cookies and account)."""

import asyncio
import logging
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from yt_dlp.utils import DownloadError

from savetube import cookies, limits, logs
from savetube.config import Settings
from savetube.db.models import Membership, SourceKind
from savetube.layout import VideoType, classify
from savetube.logs import emit
from savetube.pipeline import get_or_create_channel
from savetube.sources import InvalidSourceUrl, Tab
from savetube.sources import parse as parse_source_url
from savetube.ytdl import avatar_url

log = logging.getLogger(__name__)

# A URL, a writable cookie file copy or None, and a cap on the entries listed
# or None for all of them. savetube.ytdl.list_entries in production.
Lister = Callable[[str, Path | None, int | None], dict[str, Any]]
Listing = dict[str, Any] | None

# Listings one scan runs at once. A listing holds a yt-dlp extraction and
# YouTube requests for its whole run.
LISTINGS = 2
# The web waits 120 s for the scan (web.api.scan.CHANNEL_SCAN_TIMEOUT). The
# listings get LIST_SECONDS, and the membership checks the time left up to
# SCAN_SECONDS, so the answer reaches the web before it gives up.
LIST_SECONDS = 80
SCAN_SECONDS = 110
# Entries per tab in a channel scan: one page of YouTube's tab listing.
SCAN_PAGE = 30
# yt-dlp's answer for a tab the channel lacks, such as Shorts on a channel without any.
_NO_TAB = "does not have a"
SCAN_TABS = (Tab.VIDEOS, Tab.SHORTS, Tab.STREAMS)


class ScanFailed(Exception):
    """The cookie-free listing failed, so the scan has no channel to show."""


def playlists(listing: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
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


def _listing(
    lister: Lister, settings: Settings, url: str, profile: str | None, limit: int | None
) -> Listing:
    """One listing in a thread. The cookie copy lives in the thread, so a
    listing the deadline drops keeps its file until it ends."""
    try:
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            return lister(url, cookies.cookiefile(settings, profile, Path(private)), limit)
    except DownloadError as e:
        if _NO_TAB in str(e):
            return None
        raise


async def _list(
    slots: asyncio.Semaphore,
    lister: Lister,
    settings: Settings,
    url: str,
    profile: str | None,
    limit: int | None,
) -> Listing:
    async with slots:
        # On a timeout the thread runs on, and its result is dropped.
        return await asyncio.to_thread(_listing, lister, settings, url, profile, limit)


async def _profile_tabs(
    slots: asyncio.Semaphore,
    lister: Lister,
    settings: Settings,
    base: str,
    profile: str,
    deadline: float,
) -> list[Listing] | None:
    """The profile's listing of each tab, None when one failed or missed the
    deadline: the cookie-free listing still shows the channel."""
    try:
        async with asyncio.timeout_at(deadline), asyncio.TaskGroup() as tg:
            tabs = [
                tg.create_task(_list(slots, lister, settings, f"{base}/{t}", profile, SCAN_PAGE))
                for t in SCAN_TABS
            ]
    except (TimeoutError, ExceptionGroup) as e:
        failure = e.exceptions[0] if isinstance(e, ExceptionGroup) else e
        emit(
            log,
            logging.WARNING,
            "scan.profile_failed",
            url=base,
            profile=profile,
            reason=logs.cut(f"{type(failure).__name__}: {failure}"),
            exc_info=not isinstance(failure, DownloadError | TimeoutError),
        )
        return None
    return [t.result() for t in tabs]


def _merge(listings: list[Listing]) -> list[dict[str, Any]] | None:
    """One tab's entries from every listing, each id once in the order first
    listed. None when no listing found the tab."""
    found = [x for x in listings if x is not None]
    if not found:
        return None
    entries: dict[str, dict[str, Any]] = {}
    for listing in found:
        for e in listing.get("entries") or []:
            if e.get("id"):
                entries.setdefault(e["id"], e)
    return list(entries.values())


def _members_only(listings: list[Listing]) -> list[str]:
    """The ids of the members-only entries the listings show, each once."""
    ids = [
        e["id"]
        for x in listings
        if x is not None
        for e in x.get("entries") or []
        if e.get("id") and classify(e) is VideoType.MEMBERS
    ]
    return list(dict.fromkeys(ids))


async def scan_channel(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    base: str,
    *,
    lister: Lister,
    prober: cookies.Prober,
) -> dict[str, Any]:
    """List the first page of each video tab without cookies and with each
    cookie profile, and every playlist without cookies. A members-only entry
    stores the channel row and checks its due memberships with that entry.
    Raises TimeoutError or ScanFailed when the cookie-free listing fails."""
    started = time.monotonic()
    present = cookies.profiles(settings)
    async with sessionmaker() as session:
        if limits.blocked(await limits.load(session), datetime.now(UTC)) is not None:
            # The member accounts make no request while a YouTube block holds.
            present = []
    slots = asyncio.Semaphore(LISTINGS)
    deadline = asyncio.get_running_loop().time() + LIST_SECONDS
    # Made first, so the cookie-free listings take the first slots.
    free = asyncio.gather(
        *(_list(slots, lister, settings, f"{base}/{t}", None, SCAN_PAGE) for t in SCAN_TABS),
        _list(slots, lister, settings, f"{base}/{Tab.PLAYLISTS}", None, None),
    )
    member_tasks = {
        p: asyncio.create_task(_profile_tabs(slots, lister, settings, base, p, deadline))
        for p in present
    }
    try:
        async with asyncio.timeout_at(deadline):
            *free_tabs, listed = await free
    except BaseException as e:
        for t in member_tasks.values():
            t.cancel()
        if isinstance(e, DownloadError):
            raise ScanFailed(str(e)) from None
        raise
    by_profile = {p: await t for p, t in member_tasks.items()}

    # Per tab, the cookie-free listing first, then each profile's by name.
    tab_listings = [
        [free, *(tabs[i] for tabs in by_profile.values() if tabs is not None)]
        for i, free in enumerate(free_tabs)
    ]
    every = [x for listings in tab_listings for x in listings]
    owner = next((x for x in (*every, listed) if x and x.get("channel_id")), None)
    if owner is None:
        raise ScanFailed("the channel lists no tab")
    channel_id = owner["channel_id"]
    members_only = _members_only(every)
    if members_only and present:
        async with sessionmaker() as session:
            await get_or_create_channel(session, owner)
            await session.commit()
        try:
            async with asyncio.timeout(max(SCAN_SECONDS - (time.monotonic() - started), 0)):
                await cookies.check_memberships(
                    sessionmaker, settings, {channel_id: members_only[0]}, prober=prober
                )
        except TimeoutError:
            # The next poll that lists a members-only entry checks the rest.
            emit(log, logging.WARNING, "scan.checks_cut", url=base, channel=channel_id)
    async with sessionmaker() as session:
        results = dict(
            (
                await session.execute(
                    select(Membership.profile, Membership.is_member).where(
                        Membership.channel_id == channel_id
                    )
                )
            ).all()
        )
    return {
        "url": base,
        "channel_id": channel_id,
        "handle": owner.get("uploader_id"),
        "name": owner.get("channel") or owner.get("uploader") or channel_id,
        "avatar_url": next((a for x in every if x and (a := avatar_url(x))), None),
        "tabs": {
            t.value: None
            if (entries := _merge(listings)) is None
            else [{"video_id": e["id"], "title": e.get("title") or e["id"]} for e in entries]
            for t, listings in zip(SCAN_TABS, tab_listings, strict=True)
        },
        "playlists": [
            {"id": e["id"], "title": e.get("title") or e["id"], "url": u}
            for e, u in playlists(listed or {})
        ],
        "memberships": [
            {
                "profile": p,
                "members_only": None if tabs is None else len(_members_only(tabs)),
                "is_member": results.get(p),
            }
            for p, tabs in by_profile.items()
        ],
    }
