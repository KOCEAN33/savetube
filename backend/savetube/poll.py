"""Source polling: list a channel tab or playlist, record what it references, and
queue an archive job for every listed video not yet in the tree."""

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube.config import Settings
from savetube.db.models import (
    Job,
    JobKind,
    JobState,
    Rule,
    Source,
    SourceKind,
    Video,
    VideoSource,
    VideoState,
)
from savetube.layout import classify
from savetube.pipeline import get_or_create_channel
from savetube.ytdl import copy_cookies

log = logging.getLogger(__name__)

# Failed archive jobs per video before polls stop queuing it again.
MAX_ARCHIVE_ATTEMPTS = 3
# Live detection owns these. An archive download of one would wait or fail.
_UNFINISHED_LIVE = frozenset({"is_live", "is_upcoming", "post_live"})

Lister = Callable[[str, Path | None], dict[str, Any]]
# Puts one `job` row on the queue and returns the queue's own job id.
Enqueue = Callable[[int], Awaitable[int]]


async def due_sources(session: AsyncSession, now: datetime) -> list[int]:
    interval = func.make_interval(0, 0, 0, 0, 0, Source.poll_interval_minutes)
    q = (
        select(Source.id)
        .where(
            Source.enabled,
            (Source.last_polled_at.is_(None)) | (Source.last_polled_at + interval <= now),
        )
        .order_by(Source.last_polled_at.asc().nulls_first(), Source.id)
    )
    return list(await session.scalars(q))


def _wanted(entry: dict[str, Any], rule: Rule, from_shorts_tab: bool) -> bool:
    if entry.get("live_status") in _UNFINISHED_LIVE:
        return False
    if classify(entry, from_shorts_tab=from_shorts_tab).value not in rule.types:
        return False
    # The flat timestamp comes from "3 weeks ago", so the cutoff is as coarse
    # as YouTube's relative date. An entry without one passes.
    ts = entry.get("timestamp")
    if rule.date_after and ts is not None:
        return datetime.fromtimestamp(ts, UTC).date() >= rule.date_after
    return True


async def sync(
    session: AsyncSession, source: Source, rule: Rule, listing: dict[str, Any]
) -> list[int]:
    """Record the listing and return the ids of new archive jobs. The caller commits."""
    shorts = source.url.endswith("/shorts")
    # A tab's entries carry no channel. The listing itself does.
    tab_channel = listing.get("channel_id") if source.kind is SourceKind.TAB else None
    if tab_channel:
        await get_or_create_channel(session, listing)
        source.channel_id = tab_channel

    entries: dict[str, dict[str, Any]] = {}
    for e in listing.get("entries") or []:
        channel_id = e.get("channel_id") or tab_channel
        # A private or deleted video in a playlist lists with no channel.
        if e.get("id") and channel_id and _wanted(e, rule, shorts):
            entries[e["id"]] = {**e, "channel_id": channel_id}
    ids = list(entries)
    if not ids:
        return []

    videos = {v.id: v for v in await session.scalars(select(Video).where(Video.id.in_(ids)))}
    new_channels = {e["channel_id"]: e for vid, e in entries.items() if vid not in videos}
    new_channels.pop(tab_channel, None)
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

    linked = set(
        await session.scalars(
            select(VideoSource.video_id).where(
                VideoSource.source_id == source.id, VideoSource.video_id.in_(ids)
            )
        )
    )
    session.add_all(VideoSource(video_id=v, source_id=source.id) for v in ids if v not in linked)

    active = set(
        await session.scalars(
            # A record job does not count: until a recorder exists, the VOD is
            # the only copy of a detected live.
            select(Job.video_id).where(
                Job.video_id.in_(ids),
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
                    Job.video_id.in_(ids),
                    Job.kind == JobKind.ARCHIVE,
                    Job.state == JobState.FAILED,
                )
                .group_by(Job.video_id)
            )
        ).all()
    )
    jobs = [
        Job(kind=JobKind.ARCHIVE, video_id=vid)
        for vid, video in videos.items()
        if video.state is not VideoState.COMPLETE
        and vid not in active
        and failures.get(vid, 0) < MAX_ARCHIVE_ATTEMPTS
    ]
    session.add_all(jobs)
    await session.flush()
    return [j.id for j in jobs]


async def poll_source(
    sessionmaker: async_sessionmaker,
    source_id: int,
    settings: Settings,
    *,
    lister: Lister,
    enqueue: Enqueue,
) -> None:
    async with sessionmaker() as session:
        source = await session.get(Source, source_id)
        if source is None:  # deleted since due_sources
            return
        rule = await session.get(Rule, source.rule_id)
        url, use_cookies = source.url, rule.use_cookies

    now = datetime.now(UTC)
    try:
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            cookiefile = None
            if use_cookies:
                if settings.cookies_file is None:
                    raise RuntimeError("the rule uses cookies and SAVETUBE_COOKIES_FILE is unset")
                cookiefile = copy_cookies(settings.cookies_file, Path(private))
            listing = await asyncio.to_thread(lister, url, cookiefile)
        async with sessionmaker() as session:
            source = await session.get(Source, source_id)
            rule = await session.get(Rule, source.rule_id)
            job_ids = await sync(session, source, rule, listing)
            source.last_polled_at = now
            source.last_error = None
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
