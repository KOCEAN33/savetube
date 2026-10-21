"""Source polling: list a channel tab or playlist, record what it references, and
queue an archive job for every listed video not yet in the tree. A channel's
playlists source adds a playlist source per playlist instead."""

import asyncio
import logging
import random
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from yt_dlp.utils import DownloadError

from savetube import cookies, logs, words
from savetube.config import Settings
from savetube.db.models import (
    Channel,
    Job,
    JobKind,
    JobState,
    ListingEntry,
    Source,
    SourceKind,
    Video,
    VideoSource,
    VideoState,
)
from savetube.layout import VideoType, classify
from savetube.logs import emit, human_duration
from savetube.pipeline import get_or_create_channel
from savetube.sources import InvalidSourceUrl
from savetube.sources import parse as parse_source_url
from savetube.ytdl import avatar_url

log = logging.getLogger(__name__)

# Failed archive jobs per video before polls stop queuing it again.
MAX_ARCHIVE_ATTEMPTS = 3
# Live detection owns these. An archive download of one would wait or fail.
_UNFINISHED_LIVE = frozenset({"is_live", "is_upcoming", "post_live"})

Lister = Callable[[str, Path | None], dict[str, Any]]
# Cancels a queue job that no worker has taken. False when one already has.
Cancel = Callable[[int], Awaitable[bool]]

NO_OWNER = "playlist has no owner channel; turn on other channels to archive it"
UNLISTED = "playlist no longer listed on the channel"


async def due_sources(session: AsyncSession, now: datetime) -> list[int]:
    interval = func.make_interval(0, 0, 0, 0, 0, Source.poll_interval_minutes)
    parent = aliased(Source)
    q = (
        select(Source.id)
        .outerjoin(parent, parent.id == Source.parent_id)
        .where(
            Source.enabled,
            # A disabled `playlists` source holds its children too.
            Source.parent_id.is_(None) | parent.enabled,
            (Source.last_polled_at.is_(None)) | (Source.last_polled_at + interval <= now),
        )
        .order_by(Source.last_polled_at.asc().nulls_first(), Source.id)
    )
    return list(await session.scalars(q))


# Why a listed entry does not pass, named as `poll.done` counts it.
SKIPS = ("words", "members", "other", "date", "live", "private")


def _skip(
    entry: dict[str, Any],
    source: Source,
    channel: Channel | None,
    lists: words.Lists,
    members: set[str],
) -> str | None:
    """None when the entry passes, else the reason it does not. `members` holds
    the channels a present cookie profile is a confirmed member of."""
    if entry.get("live_status") in _UNFINISHED_LIVE:
        return "live"
    if not words.matches(entry.get("title") or "", *lists):
        return "words"
    # Without a member's cookies a members-only download fails every attempt.
    if classify(entry) is VideoType.MEMBERS and entry["channel_id"] not in members:
        return "members"
    if (
        source.kind is SourceKind.PLAYLIST
        and not source.other_channels
        and entry["channel_id"] != source.channel_id
    ):
        return "other"
    # The flat timestamp comes from "3 weeks ago", so the cutoff is as coarse
    # as YouTube's relative date. An entry without one passes.
    ts = entry.get("timestamp")
    if channel is not None and channel.date_after and ts is not None:
        if datetime.fromtimestamp(ts, UTC).date() < channel.date_after:
            return "date"
    return None


@dataclass
class Synced:
    """What one poll did, for `poll.done`."""

    # New archive jobs, and queued archive jobs whose video no longer passes,
    # each with its video id and skip reason.
    job_ids: list[int] = field(default_factory=list)
    drops: list[tuple[int, str, str]] = field(default_factory=list)
    listed: int = 0
    passed: int = 0
    stored: int = 0
    in_flight: int = 0
    gave_up: int = 0
    skips: Counter[str] = field(default_factory=Counter)
    titles: dict[str, str] = field(default_factory=dict)
    # Set when the poll queued nothing on purpose: "archiving_off", "no_owner",
    # "playlists".
    mode: str | None = None
    playlists: int = 0
    added: int = 0
    disabled: list[Source] = field(default_factory=list)
    # One listed members-only video id per channel, for the membership checks.
    members_only: dict[str, str] = field(default_factory=dict)


async def sync(
    session: AsyncSession, source: Source, listing: dict[str, Any], profiles: list[str]
) -> Synced:
    """Record the listing. Return the new archive jobs, and the queued archive
    jobs whose video no longer passes. `profiles` are the present cookie
    profiles. The caller commits, then cancels."""
    parent = await session.get(Source, source.parent_id) if source.parent_id else None
    if parent is not None:
        # A child keeps its parent's channel, whatever the listing says.
        source.channel_id = parent.channel_id
    elif listing.get("channel_id"):
        channel = await get_or_create_channel(session, listing)
        source.channel_id = listing["channel_id"]
        # A tab listing carries the channel's own thumbnails. A playlist's are
        # the playlist's.
        if source.kind is SourceKind.TAB and (avatar := avatar_url(listing)):
            channel.avatar_url = avatar
    if source.kind is SourceKind.PLAYLISTS:
        return await _add_playlists(session, source, listing)
    if source.kind is SourceKind.PLAYLIST and listing.get("title"):
        source.title = listing["title"]

    shorts = source.url.endswith("/shorts")
    # A tab's entries carry no channel. The listing itself does.
    tab_channel = source.channel_id if source.kind is SourceKind.TAB else None
    listed: dict[str, dict[str, Any]] = {}
    for e in listing.get("entries") or []:
        if e.get("id"):
            listed[e["id"]] = {**e, "channel_id": e.get("channel_id") or tab_channel}
    result = Synced(listed=len(listed))
    for vid, e in listed.items():
        if e["channel_id"] and classify(e) is VideoType.MEMBERS:
            result.members_only.setdefault(e["channel_id"], vid)
    if not listed:
        # Keeps the last titles for suggestions and the preview.
        return result
    await _replace_listing(session, source.id, listed)
    ids = list(listed)
    videos = {v.id: v for v in await session.scalars(select(Video).where(Video.id.in_(ids)))}

    if source.kind is SourceKind.PLAYLIST and not source.other_channels and not source.channel_id:
        source.last_error = NO_OWNER
        await _link(session, source.id, list(videos))
        result.mode = "no_owner"
        return result
    if not source.archiving:
        # Listing first: the user sets words against the preview before any download.
        await _link(session, source.id, list(videos))
        result.mode = "archiving_off"
        return result

    own = [(parent.include_words, parent.exclude_words)] if parent is not None else []
    own.append((source.include_words, source.exclude_words))
    # The channel level is the uploader's, so another channel's video in a
    # playlist meets its own channel's words.
    lists = {
        cid: await words.effective(session, cid, *own)
        for cid in {e["channel_id"] for e in listed.values()}
    }
    # The source's channel holds the date cutoff, also for another channel's
    # video in its playlist.
    channel = await session.get(Channel, source.channel_id) if source.channel_id else None
    members = await cookies.member_channels(session, list(result.members_only), profiles)
    entries: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    for vid, e in listed.items():
        # A private or deleted video in a playlist lists with no channel.
        reason = (
            _skip(e, source, channel, lists[e["channel_id"]], members)
            if e["channel_id"]
            else "private"
        )
        if reason is None:
            entries[vid] = e
        else:
            skipped[vid] = reason
    result.passed = len(entries)
    result.skips = Counter(skipped.values())
    result.titles = {vid: e.get("title") or "" for vid, e in listed.items()}

    new_channels = {e["channel_id"]: e for vid, e in entries.items() if vid not in videos}
    new_channels.pop(source.channel_id, None)
    for e in new_channels.values():
        await get_or_create_channel(session, e)
    # No relationship() orders inserts, so each FK target is flushed first.
    await session.flush()

    for vid, e in entries.items():
        if vid not in videos:
            videos[vid] = Video(
                id=vid,
                channel_id=e["channel_id"],
                type=classify(e, from_shorts_tab=shorts),
                title=e.get("title") or vid,
                state=VideoState.PENDING,
            )
            session.add(videos[vid])
    await session.flush()
    # Membership is recorded for every stored video, the filtered ones too.
    await _link(session, source.id, list(videos))

    wanted = list(entries)
    active = set(
        await session.scalars(
            # A record job does not count: until a recorder exists, the VOD is
            # the only copy of a detected live.
            select(Job.video_id).where(
                Job.video_id.in_(wanted),
                Job.kind == JobKind.ARCHIVE,
                Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
            )
        )
    )
    failures = dict(
        (
            await session.execute(
                select(Job.video_id, func.count())
                .where(
                    Job.video_id.in_(wanted),
                    Job.kind == JobKind.ARCHIVE,
                    Job.state == JobState.FAILED,
                )
                .group_by(Job.video_id)
            )
        ).all()
    )
    jobs = []
    for vid in wanted:
        if videos[vid].state is VideoState.COMPLETE:
            result.stored += 1
        elif vid in active:
            result.in_flight += 1
        elif failures.get(vid, 0) >= MAX_ARCHIVE_ATTEMPTS:
            result.gave_up += 1
        else:
            jobs.append(Job(kind=JobKind.ARCHIVE, video_id=vid))
    session.add_all(jobs)
    await session.flush()
    result.job_ids = [j.id for j in jobs]
    result.drops = [
        (job_id, vid, skipped[vid])
        for job_id, vid in await _droppable(session, source.id, set(skipped))
    ]
    return result


async def _link(session: AsyncSession, source_id: int, video_ids: list[str]) -> None:
    linked = set(
        await session.scalars(
            select(VideoSource.video_id).where(
                VideoSource.source_id == source_id, VideoSource.video_id.in_(video_ids)
            )
        )
    )
    session.add_all(
        VideoSource(video_id=v, source_id=source_id) for v in video_ids if v not in linked
    )
    await session.flush()


async def _droppable(
    session: AsyncSession, source_id: int, skipped: set[str]
) -> list[tuple[int, str]]:
    """Queued archive jobs of videos this source's filters now skip. A video that
    another source lists is left alone: that source may still want it, and
    cancelling would race its next poll."""
    elsewhere = select(VideoSource.video_id).where(VideoSource.source_id != source_id)
    rows = await session.execute(
        select(Job.id, Job.video_id).where(
            Job.video_id.in_(skipped),
            Job.video_id.not_in(elsewhere),
            Job.kind == JobKind.ARCHIVE,
            Job.state == JobState.QUEUED,
        )
    )
    return [(r.id, r.video_id) for r in rows]


async def _replace_listing(
    session: AsyncSession, source_id: int, listed: dict[str, dict[str, Any]]
) -> None:
    """Title suggestions and the preview read every listed title, the skipped ones too."""
    await session.execute(delete(ListingEntry).where(ListingEntry.source_id == source_id))
    await session.execute(
        insert(ListingEntry),
        [
            {
                "source_id": source_id,
                "video_id": vid,
                "channel_id": e["channel_id"],
                "title": e.get("title") or "",
            }
            for vid, e in listed.items()
        ],
    )


async def _add_playlists(session: AsyncSession, parent: Source, listing: dict[str, Any]) -> Synced:
    """A child playlist source per listed playlist that is not a source yet. A
    disabled child is still a source, so it is never created again. A child
    whose playlist left the listing is disabled."""
    titles: dict[str, str | None] = {}
    for e in listing.get("entries") or []:
        try:
            [parsed] = parse_source_url(e.get("url") or "")
        except InvalidSourceUrl:
            continue
        if parsed.kind is SourceKind.PLAYLIST:
            titles.setdefault(parsed.url, e.get("title"))
    urls = list(titles)
    result = Synced(mode="playlists", playlists=len(urls))
    if not urls:
        # An empty listing proves nothing about the channel's playlists.
        return result
    for child in await session.scalars(select(Source).where(Source.parent_id == parent.id)):
        if child.url in titles:
            child.title = titles[child.url] or child.title
        elif child.enabled:
            child.enabled = False
            child.last_error = UNLISTED
            result.disabled.append(child)
    taken = set(await session.scalars(select(Source.url).where(Source.url.in_(urls))))
    now = datetime.now(UTC)
    interval = timedelta(minutes=parent.poll_interval_minutes)
    result.added = len(set(urls) - taken)
    session.add_all(
        Source(
            url=url,
            title=titles[url],
            kind=SourceKind.PLAYLIST,
            channel_id=parent.channel_id,
            poll_interval_minutes=parent.poll_interval_minutes,
            other_channels=parent.other_channels,
            archiving=parent.archiving,
            parent_id=parent.id,
            # Spreads the first polls of many new children over one interval.
            last_polled_at=now - interval * random.random(),
        )
        for url in urls
        if url not in taken
    )
    await session.flush()
    return result


async def poll_source(
    sessionmaker: async_sessionmaker,
    source_id: int,
    settings: Settings,
    *,
    lister: Lister,
    cancel: Cancel,
    prober: cookies.Prober,
) -> None:
    # yt-dlp's own lines during the listing carry the source too.
    with logs.context(source=source_id):
        await _poll_source(
            sessionmaker, source_id, settings, lister=lister, cancel=cancel, prober=prober
        )


async def _poll_source(
    sessionmaker: async_sessionmaker,
    source_id: int,
    settings: Settings,
    *,
    lister: Lister,
    cancel: Cancel,
    prober: cookies.Prober,
) -> None:
    async with sessionmaker() as session:
        source = await session.get(Source, source_id)
        if source is None:  # deleted since due_sources
            return
        channel = await session.get(Channel, source.channel_id) if source.channel_id else None
        url, profile = source.url, await cookies.general(session)
        emit(
            log,
            logging.INFO,
            "poll.start",
            source=source_id,
            url=url,
            kind=source.kind.value,
            channel=channel.name if channel is not None else None,
        )

    now = datetime.now(UTC)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            cookiefile = cookies.cookiefile(settings, profile, Path(private))
            listing = await asyncio.to_thread(lister, url, cookiefile)
        async with sessionmaker() as session:
            source = await session.get(Source, source_id)
            source.last_polled_at = now
            source.last_error = None
            synced = await sync(session, source, listing, cookies.profiles(settings))
            await session.commit()
    except Exception as e:
        expected = isinstance(e, DownloadError)
        emit(
            log,
            logging.WARNING if expected else logging.ERROR,
            "poll.failed",
            source=source_id,
            url=url,
            reason=logs.cut(f"{type(e).__name__}: {e}"),
            took=human_duration(time.monotonic() - started),
            exc_info=not expected,
        )
        async with sessionmaker() as session:
            source = await session.get(Source, source_id)
            source.last_polled_at = now
            source.last_error = f"{type(e).__name__}: {e}"
            await session.commit()
        return

    # The scheduler's dispatcher puts the new jobs on the queue.
    cancelled = 0
    for job_id, video_id, reason in synced.drops:
        cancelled += await _cancel_job(
            sessionmaker,
            job_id,
            cancel,
            video=video_id,
            title=synced.titles.get(video_id),
            source=source_id,
            reason=reason,
        )
    _log_done(source_id, url, synced, cancelled, time.monotonic() - started)
    await cookies.check_memberships(sessionmaker, settings, synced.members_only, prober=prober)


def _log_done(source_id: int, url: str, synced: Synced, cancelled: int, took: float) -> None:
    if synced.mode == "playlists":
        for child in synced.disabled:
            emit(
                log,
                logging.WARNING,
                "playlist.unlisted",
                source=child.id,
                parent=source_id,
                title=child.title,
            )
        emit(
            log,
            logging.INFO,
            "poll.done",
            source=source_id,
            playlists=synced.playlists,
            added=synced.added,
            disabled=len(synced.disabled),
            took=human_duration(took),
        )
        return
    if synced.mode == "no_owner":
        emit(log, logging.WARNING, "poll.no_owner", source=source_id, url=url, reason=NO_OWNER)
    if synced.mode is not None:
        emit(
            log,
            logging.INFO,
            "poll.done",
            source=source_id,
            listed=synced.listed,
            archiving="off" if synced.mode == "archiving_off" else None,
            took=human_duration(took),
        )
        return
    emit(
        log,
        logging.INFO,
        "poll.done",
        source=source_id,
        listed=synced.listed,
        passed=synced.passed,
        queued=len(synced.job_ids),
        stored=synced.stored,
        in_flight=synced.in_flight,
        gave_up=synced.gave_up,
        cancelled=cancelled,
        **{f"skip_{reason}": synced.skips[reason] for reason in SKIPS},
        took=human_duration(took),
    )


async def _cancel_job(
    sessionmaker: async_sessionmaker,
    job_id: int,
    cancel: Cancel,
    *,
    video: str,
    title: str | None,
    source: int,
    reason: str,
) -> bool:
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job.state is not JobState.QUEUED:
            return False
        if job.queue_job_id is not None:
            try:
                if not await cancel(job.queue_job_id):
                    return False  # a worker took it first
            except Exception:
                emit(log, logging.ERROR, "job.cancel_failed", job=job_id, exc_info=True)
                return False
        job.state = JobState.CANCELLED
        job.finished_at = datetime.now(UTC)
        await session.commit()
    emit(
        log,
        logging.INFO,
        "job.cancelled",
        job=job_id,
        video=video,
        title=logs.cut(title, 80),
        source=source,
        reason=reason,
    )
    return True
