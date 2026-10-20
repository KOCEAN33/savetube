"""Live detection for watched channels: the RSS feed finds new and upcoming
video ids, and the channel's `/live` page finds a stream with no waiting room.

A detection is a queued `record` job. Nothing starts a recorder yet.
"""

import asyncio
import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yt_dlp.utils import DownloadError

from savetube.db.models import Job, JobKind, Rule, Source, SourceKind, Video, VideoState
from savetube.layout import VideoType
from savetube.pipeline import get_or_create_channel

log = logging.getLogger(__name__)

FetchFeed = Callable[[str], str]
Probe = Callable[[str], dict[str, Any]]

_YT = "{http://www.youtube.com/xml/schemas/2015}"
_ATOM = "{http://www.w3.org/2005/Atom}"
_DETECTED = frozenset({"is_upcoming", "is_live"})
_NOT_LIVE = "is not currently live"


def parse_feed(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [vid for e in root.iter(f"{_ATOM}entry") if (vid := e.findtext(f"{_YT}videoId"))]


async def watched_channels(session: AsyncSession) -> list[str]:
    q = (
        select(Source.channel_id)
        .join(Rule, Rule.id == Source.rule_id)
        .where(
            Source.enabled,
            Source.kind == SourceKind.TAB,
            Source.channel_id.is_not(None),
            Rule.watch_lives,
        )
        .distinct()
        .order_by(Source.channel_id)
    )
    return list(await session.scalars(q))


async def _record(session: AsyncSession, info: dict[str, Any]) -> Job | None:
    """A queued record job for an upcoming or live stream, unless one exists."""
    if info.get("live_status") not in _DETECTED:
        return None
    video_id = info["id"]
    existing = await session.scalar(
        select(Job.id).where(Job.video_id == video_id, Job.kind == JobKind.RECORD).limit(1)
    )
    if existing is not None:
        return None
    if await session.get(Video, video_id) is None:
        await get_or_create_channel(session, info)
        # No relationship() orders inserts, so each FK target is flushed first.
        await session.flush()
        session.add(
            Video(
                id=video_id,
                channel_id=info["channel_id"],
                type=VideoType.LIVES,
                title=info.get("title") or video_id,
                state=VideoState.PENDING,
            )
        )
        await session.flush()
    start = info.get("release_timestamp")
    job = Job(
        kind=JobKind.RECORD,
        video_id=video_id,
        scheduled_at=(
            datetime.fromtimestamp(start, UTC)
            if start is not None and info["live_status"] == "is_upcoming"
            else None
        ),
    )
    session.add(job)
    await session.flush()
    return job


async def check_channel(
    sessionmaker: async_sessionmaker,
    channel_id: str,
    *,
    fetch_feed: FetchFeed,
    probe: Probe,
    probe_live: Probe,
    list_streams: Probe,
    seen: set[str],
) -> list[int]:
    """Probe unseen ids from the feed and the Live tab, and the `/live` page;
    return new record job ids.

    `seen` lives in the scheduler process, so a restart probes each feed's
    ids once more. The record job, not `seen`, keeps detections unique.
    """
    candidates = parse_feed(await asyncio.to_thread(fetch_feed, channel_id))
    # The feed holds the 15 newest uploads, so a busy channel pushes a waiting
    # room out of it. The Live tab lists it until the stream ends.
    streams = await asyncio.to_thread(list_streams, channel_id)
    candidates += [
        e["id"] for e in streams.get("entries") or [] if e.get("live_status") in _DETECTED
    ]
    found: list[dict[str, Any]] = []
    for video_id in dict.fromkeys(candidates):
        if video_id in seen:
            continue
        seen.add(video_id)
        try:
            found.append(await asyncio.to_thread(probe, video_id))
        except DownloadError as e:
            # A members-only or private video stays unreadable, so it is not
            # probed again until the scheduler restarts.
            log.warning("probe of %s failed: %s", video_id, e)
    try:
        found.append(await asyncio.to_thread(probe_live, channel_id))
    except DownloadError as e:
        if _NOT_LIVE not in str(e):
            raise

    created: list[int] = []
    async with sessionmaker() as session:
        for info in found:
            job = await _record(session, info)
            if job is not None:
                created.append(job.id)
                log.info("live detected: %s (%s)", info["id"], info["live_status"])
        await session.commit()
    return created
