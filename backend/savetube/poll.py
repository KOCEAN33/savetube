"""Source polling: list a channel tab or playlist, record what it references, and
queue an archive job for every listed video not yet in the tree. A channel's
playlists source adds a playlist source per playlist instead."""

import asyncio
import logging
import random
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from savetube import words
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
from savetube.layout import classify
from savetube.pipeline import get_or_create_channel
from savetube.sources import InvalidSourceUrl
from savetube.sources import parse as parse_source_url
from savetube.ytdl import copy_cookies

log = logging.getLogger(__name__)

# Failed archive jobs per video before polls stop queuing it again.
MAX_ARCHIVE_ATTEMPTS = 3
# Live detection owns these. An archive download of one would wait or fail.
_UNFINISHED_LIVE = frozenset({"is_live", "is_upcoming", "post_live"})

Lister = Callable[[str, Path | None], dict[str, Any]]
# Puts one `job` row on the queue and returns the queue's own job id.
Enqueue = Callable[[int], Awaitable[int]]
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


def _wanted(
    entry: dict[str, Any],
    source: Source,
    channel: Channel | None,
    lists: words.Lists,
) -> bool:
    if entry.get("live_status") in _UNFINISHED_LIVE:
        return False
    if not words.matches(entry.get("title") or "", *lists):
        return False
    if (
        source.kind is SourceKind.PLAYLIST
        and not source.other_channels
        and entry["channel_id"] != source.channel_id
    ):
        return False
    # The flat timestamp comes from "3 weeks ago", so the cutoff is as coarse
    # as YouTube's relative date. An entry without one passes.
    ts = entry.get("timestamp")
    if channel is not None and channel.date_after and ts is not None:
        return datetime.fromtimestamp(ts, UTC).date() >= channel.date_after
    return True


async def sync(
    session: AsyncSession, source: Source, listing: dict[str, Any]
) -> tuple[list[int], list[int]]:
    """Record the listing. Return the ids of new archive jobs, and of queued archive
    jobs whose video no longer passes. The caller commits, then queues and cancels."""
    parent = await session.get(Source, source.parent_id) if source.parent_id else None
    if parent is not None:
        # A child keeps its parent's channel, whatever the listing says.
        source.channel_id = parent.channel_id
    elif listing.get("channel_id"):
        await get_or_create_channel(session, listing)
        source.channel_id = listing["channel_id"]
    if source.kind is SourceKind.PLAYLISTS:
        await _add_playlists(session, source, listing)
        return [], []
    if source.kind is SourceKind.PLAYLIST and listing.get("title"):
        source.title = listing["title"]

    shorts = source.url.endswith("/shorts")
    # A tab's entries carry no channel. The listing itself does.
    tab_channel = source.channel_id if source.kind is SourceKind.TAB else None
    listed: dict[str, dict[str, Any]] = {}
    for e in listing.get("entries") or []:
        if e.get("id"):
            listed[e["id"]] = {**e, "channel_id": e.get("channel_id") or tab_channel}
    if not listed:
        # Keeps the last titles for suggestions and the preview.
        return [], []
    await _replace_listing(session, source.id, listed)
    ids = list(listed)
    videos = {v.id: v for v in await session.scalars(select(Video).where(Video.id.in_(ids)))}

    if source.kind is SourceKind.PLAYLIST and not source.other_channels and not source.channel_id:
        source.last_error = NO_OWNER
        await _link(session, source.id, list(videos))
        return [], []
    if not source.archiving:
        # Listing first: the user sets words against the preview before any download.
        await _link(session, source.id, list(videos))
        return [], []

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
    # A private or deleted video in a playlist lists with no channel.
    entries = {
        vid: e
        for vid, e in listed.items()
        if e["channel_id"] and _wanted(e, source, channel, lists[e["channel_id"]])
    }

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
    jobs = [
        Job(kind=JobKind.ARCHIVE, video_id=vid)
        for vid in wanted
        if videos[vid].state is not VideoState.COMPLETE
        and vid not in active
        and failures.get(vid, 0) < MAX_ARCHIVE_ATTEMPTS
    ]
    session.add_all(jobs)
    await session.flush()
    return [j.id for j in jobs], await _droppable(session, source.id, set(ids) - entries.keys())


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


async def _droppable(session: AsyncSession, source_id: int, skipped: set[str]) -> list[int]:
    """Queued archive jobs of videos this source's filters now skip. A video that
    another source lists is left alone: that source may still want it, and
    cancelling would race its next poll."""
    elsewhere = select(VideoSource.video_id).where(VideoSource.source_id != source_id)
    return list(
        await session.scalars(
            select(Job.id).where(
                Job.video_id.in_(skipped),
                Job.video_id.not_in(elsewhere),
                Job.kind == JobKind.ARCHIVE,
                Job.state == JobState.QUEUED,
            )
        )
    )


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


async def _add_playlists(session: AsyncSession, parent: Source, listing: dict[str, Any]) -> None:
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
    if not urls:
        # An empty listing proves nothing about the channel's playlists.
        return
    for child in await session.scalars(select(Source).where(Source.parent_id == parent.id)):
        if child.url in titles:
            child.title = titles[child.url] or child.title
        elif child.enabled:
            child.enabled = False
            child.last_error = UNLISTED
    taken = set(await session.scalars(select(Source.url).where(Source.url.in_(urls))))
    now = datetime.now(UTC)
    interval = timedelta(minutes=parent.poll_interval_minutes)
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


async def poll_source(
    sessionmaker: async_sessionmaker,
    source_id: int,
    settings: Settings,
    *,
    lister: Lister,
    enqueue: Enqueue,
    cancel: Cancel,
) -> None:
    async with sessionmaker() as session:
        source = await session.get(Source, source_id)
        if source is None:  # deleted since due_sources
            return
        channel = await session.get(Channel, source.channel_id) if source.channel_id else None
        url, use_cookies = source.url, channel is not None and channel.use_cookies

    now = datetime.now(UTC)
    try:
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            cookiefile = None
            if use_cookies:
                if settings.cookies_file is None:
                    raise RuntimeError(
                        "the channel uses cookies and SAVETUBE_COOKIES_FILE is unset"
                    )
                cookiefile = copy_cookies(settings.cookies_file, Path(private))
            listing = await asyncio.to_thread(lister, url, cookiefile)
        async with sessionmaker() as session:
            source = await session.get(Source, source_id)
            source.last_polled_at = now
            source.last_error = None
            job_ids, drop_ids = await sync(session, source, listing)
            await session.commit()
    except Exception as e:
        log.exception("poll of source %s failed", source_id)
        async with sessionmaker() as session:
            source = await session.get(Source, source_id)
            source.last_polled_at = now
            source.last_error = f"{type(e).__name__}: {e}"
            await session.commit()
        return

    log.info("source %s: %d new archive jobs", source_id, len(job_ids))
    for job_id in job_ids:
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            try:
                job.queue_job_id = await enqueue(job_id)
            except Exception as e:
                # The next poll queues the video again, within MAX_ARCHIVE_ATTEMPTS.
                log.exception("enqueue of job %s failed", job_id)
                job.state = JobState.FAILED
                job.finished_at = datetime.now(UTC)
                job.log_tail = f"enqueue failed: {type(e).__name__}: {e}"
            await session.commit()
    for job_id in drop_ids:
        await _cancel_job(sessionmaker, job_id, cancel)


async def _cancel_job(sessionmaker: async_sessionmaker, job_id: int, cancel: Cancel) -> None:
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job.state is not JobState.QUEUED:
            return
        if job.queue_job_id is not None:
            try:
                if not await cancel(job.queue_job_id):
                    return  # a worker took it first
            except Exception:
                log.exception("cancel of job %s failed", job_id)
                return
        job.state = JobState.CANCELLED
        job.finished_at = datetime.now(UTC)
        await session.commit()
