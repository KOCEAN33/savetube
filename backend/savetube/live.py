"""Live detection for watched channels: the RSS feed finds new and upcoming
video ids, and the channel's `/live` page finds a stream with no waiting room.

A detection is a queued `record` job, made only for a title that passes the
words of a source watching the channel. savetube.recorders starts its recorder.
"""

import asyncio
import logging
import urllib.error
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yt_dlp.utils import DownloadError

from savetube import cookies, logs, words
from savetube.db.models import (
    Channel,
    Job,
    JobKind,
    LiveSeen,
    Source,
    SourceKind,
    Video,
    VideoState,
)
from savetube.layout import VideoType, classify
from savetube.logs import emit
from savetube.pipeline import get_or_create_channel
from savetube.tz import TIMEZONE
from savetube.ytdl import members_only

log = logging.getLogger(__name__)

FetchFeed = Callable[[str], str]
Probe = Callable[[str], dict[str, Any]]
# Probes a video id with the named cookie profile.
MemberProbe = Callable[[str, str], dict[str, Any]]
# A probed id and what the probe found, None when it failed (`live_seen`).
Seen = dict[str, dict[str, Any] | None]

_YT = "{http://www.youtube.com/xml/schemas/2015}"
_ATOM = "{http://www.w3.org/2005/Atom}"
_DETECTED = frozenset({"is_upcoming", "is_live"})
_NOT_LIVE = "is not currently live"
# An unreadable video is probed again this long after its last probe: a member
# profile or a membership may have appeared.
RETRY_UNREADABLE = timedelta(hours=24)
# Videos whose title the words skip, logged once per process.
_skip_logged: set[str] = set()
# What `_record` and the words read. A full info dict holds every format.
_KEPT = (
    "id",
    "title",
    "channel_id",
    "channel",
    "uploader",
    "uploader_id",
    "live_status",
    "media_type",
    "release_timestamp",
    "availability",
)


def parse_feed(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [vid for e in root.iter(f"{_ATOM}entry") if (vid := e.findtext(f"{_YT}videoId"))]


def _watching(q):
    return q.join(Channel, Channel.id == Source.channel_id).where(
        Source.enabled,
        Source.kind == SourceKind.TAB,
        Channel.watch_lives,
    )


async def watched_channels(session: AsyncSession) -> list[str]:
    q = _watching(select(Source.channel_id)).distinct().order_by(Source.channel_id)
    return list(await session.scalars(q))


async def _wanted(session: AsyncSession, channel_id: str, info: dict[str, Any]) -> bool:
    """Whether the stream's title passes the effective words of a source
    watching the channel. Watching `streams` tabs, when there are any, decide
    alone. A members-only stream of a channel with `members_all` on, and any
    stream of a channel with `keep_all` on, passes any watching source."""
    channel = await session.get(Channel, channel_id)
    passes = channel is not None and (
        channel.keep_all or (classify(info) is VideoType.MEMBERS and channel.members_all)
    )
    title = info.get("title") or ""
    rows = (
        await session.execute(
            _watching(select(Source.url, Source.include_words, Source.exclude_words)).where(
                Source.channel_id == channel_id
            )
        )
    ).all()
    streams = [r for r in rows if r.url.endswith("/streams")]
    for r in streams or rows:
        lists = await words.effective(session, channel_id, (r.include_words, r.exclude_words))
        if passes or words.matches(title, *lists):
            return True
    return False


def _premiere(info: dict[str, Any]) -> bool:
    """An upcoming or running premiere. yt-dlp gives it the `live_status` of a
    stream, but `media_type` `video` instead of `livestream`. A `live_seen` row
    stored before `media_type` was kept has none and counts as a stream."""
    return info.get("live_status") in _DETECTED and info.get("media_type") not in (
        None,
        "livestream",
    )


async def _record(session: AsyncSession, info: dict[str, Any]) -> Job | None:
    """A queued record job for an upcoming or live stream, unless one exists."""
    if info.get("live_status") not in _DETECTED or _premiere(info):
        return None
    video_id = info["id"]
    existing = await session.scalar(
        select(Job.id).where(Job.video_id == video_id, Job.kind == JobKind.RECORD).limit(1)
    )
    if existing is not None:
        return None
    video = await session.get(Video, video_id)
    if video is not None and video.state is VideoState.DELETED:
        return None
    if video is None:
        await get_or_create_channel(session, info)
        # No relationship() orders inserts, so each FK target is flushed first.
        await session.flush()
        session.add(
            Video(
                id=video_id,
                channel_id=info["channel_id"],
                # A members-only live records with a member profile (savetube.jobs).
                type=VideoType.MEMBERS if classify(info) is VideoType.MEMBERS else VideoType.LIVES,
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
    member_probe: MemberProbe | None = None,
    profiles: list[str] | None = None,
) -> list[int]:
    """Probe unseen ids from the feed and the Live tab, and the `/live` page;
    return new record job ids.

    `probe` reads with the general profile. A members-only refusal is probed
    once more through `member_probe` with the channel's member profile among
    the present `profiles`, if one exists.

    A probed id is a `live_seen` row, so no later check probes it again,
    across scheduler restarts too. An unreadable one is probed again after
    RETRY_UNREADABLE. The words are applied on every check, so a
    word change needs no probe. The record job keeps detections unique.
    """
    name = await _channel_name(sessionmaker, channel_id)
    candidates = parse_feed(await asyncio.to_thread(fetch_feed, channel_id))
    # The feed holds the 15 newest uploads, so a busy channel pushes a waiting
    # room out of it. The Live tab lists it until the stream ends.
    streams = await asyncio.to_thread(list_streams, channel_id)
    candidates += [
        e["id"] for e in streams.get("entries") or [] if e.get("live_status") in _DETECTED
    ]
    ids = list(dict.fromkeys(candidates))
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        rows = await session.execute(
            select(LiveSeen.video_id, LiveSeen.info, LiveSeen.probed_at).where(
                LiveSeen.video_id.in_(ids),
                or_(LiveSeen.info.is_not(None), LiveSeen.probed_at > now - RETRY_UNREADABLE),
            )
        )
        seen: Seen = {}
        probed: dict[str, datetime] = {}
        for video_id, info, probed_at in rows:
            seen[video_id] = info
            probed[video_id] = probed_at
    try:
        for video_id in ids:
            if video_id in seen:
                continue
            try:
                try:
                    info = await asyncio.to_thread(probe, video_id)
                except DownloadError as e:
                    profile = None
                    if member_probe is not None and members_only(str(e)):
                        async with sessionmaker() as session:
                            profile = await cookies.member_profile(
                                session, channel_id, profiles or []
                            )
                    if profile is None:
                        raise
                    info = await asyncio.to_thread(member_probe, video_id, profile)
                    # The general probe's refusal proves the video members-only.
                    info = {**info, "availability": "subscriber_only"}
                seen[video_id] = {k: info.get(k) for k in _KEPT}
            except DownloadError as e:
                # A members-only or private video stays unreadable, so it waits
                # RETRY_UNREADABLE for its next probe.
                emit(
                    log,
                    logging.WARNING,
                    "live.probe_failed",
                    video=video_id,
                    channel=name,
                    reason=logs.cut(str(e)),
                )
                seen[video_id] = None
    finally:
        # A check cut off by a failing probe keeps the probes made before it.
        await _remember(sessionmaker, seen, probed, now)
    found = [info for video_id in ids if (info := seen.get(video_id)) is not None]
    try:
        found.append(await asyncio.to_thread(probe_live, channel_id))
    except DownloadError as e:
        if _NOT_LIVE not in str(e):
            raise

    created: list[int] = []
    lines = []
    async with sessionmaker() as session:
        for info in found:
            premiere = _premiere(info)
            if premiere or not await _wanted(session, channel_id, info):
                if info.get("live_status") in _DETECTED and info["id"] not in _skip_logged:
                    _skip_logged.add(info["id"])
                    emit(
                        log,
                        logging.INFO,
                        "live.skipped",
                        video=info["id"],
                        channel=name,
                        title=logs.cut(info.get("title"), 80),
                        reason="premiere" if premiere else "words",
                    )
                continue
            job = await _record(session, info)
            if job is not None:
                created.append(job.id)
                start = job.scheduled_at
                lines.append(
                    dict(
                        job=job.id,
                        video=info["id"],
                        channel=name,
                        status=info["live_status"],
                        starts=start.astimezone(TIMEZONE).isoformat() if start else None,
                        title=logs.cut(info.get("title"), 80),
                    )
                )
        await session.commit()
    for fields in lines:
        emit(log, logging.INFO, "live.found", **fields)
    emit(
        log,
        logging.DEBUG,
        "live.check",
        channel=name,
        candidates=len(dict.fromkeys(candidates)),
        new=len(created),
    )
    return created


async def _remember(
    sessionmaker: async_sessionmaker, seen: Seen, probed: dict[str, datetime], now: datetime
) -> None:
    """Store every id of `seen` as listed `now`. An id missing from `probed`
    was probed in this check, at `now`."""
    if not seen:
        return
    stmt = insert(LiveSeen).values(
        [
            {"video_id": v, "info": info, "probed_at": probed.get(v, now), "listed_at": now}
            for v, info in seen.items()
        ]
    )
    excluded = stmt.excluded
    async with sessionmaker() as session:
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[LiveSeen.video_id],
                set_={
                    "info": excluded.info,
                    "probed_at": excluded.probed_at,
                    "listed_at": excluded.listed_at,
                },
            )
        )
        await session.commit()


async def _channel_name(sessionmaker: async_sessionmaker, channel_id: str) -> str:
    async with sessionmaker() as session:
        channel = await session.get(Channel, channel_id)
    return channel.name if channel is not None else channel_id


# A YouTube or network failure. The next tick tries again.
_EXPECTED = (DownloadError, urllib.error.URLError, TimeoutError, ConnectionError)


async def check_logged(
    sessionmaker: async_sessionmaker, channel_id: str, **checks: Any
) -> list[int]:
    """`check_channel`, with a failure logged as `live.check_failed`. An expected
    failure is one WARNING line. Anything else is an ERROR with its traceback."""
    name = await _channel_name(sessionmaker, channel_id)
    # yt-dlp's own lines during the probes carry the channel too.
    with logs.context(channel=name):
        try:
            return await check_channel(sessionmaker, channel_id, **checks)
        except Exception as e:
            expected = isinstance(e, _EXPECTED)
            emit(
                log,
                logging.WARNING if expected else logging.ERROR,
                "live.check_failed",
                channel=name,
                reason=logs.cut(f"{type(e).__name__}: {e}"),
                exc_info=not expected,
            )
            return []
