"""The format watch of stored lives (docs/plan/live.md, Format watch).

YouTube adds better formats to a live's VOD hours or days after the stream.
For WATCH_DAYS after a live video turns complete, the scheduler probes its
formats every PROBE_EVERY and queues a replace job for a stream that a better
one can take the place of (savetube.jobs.replace)."""

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube import cookies, formats, logs
from savetube.config import Settings
from savetube.db.models import FormatWatch, Job, JobKind, JobState, Video, VideoState
from savetube.integrity import IntegrityFailed, Tools
from savetube.layout import LIVE_STATUSES, VideoType
from savetube.logs import emit

log = logging.getLogger(__name__)

WATCH_DAYS = timedelta(days=7)
PROBE_EVERY = timedelta(hours=6)
# Where a replace moves the first stored file, on the archive's filesystem,
# so the move is a hard link.
ORIGINALS_DIR = ".originals"

# Reads a video id's info dict, formats included, with an optional cookie file.
Probe = Callable[[str, Path | None], dict[str, Any]]

_ACTIVE = (JobState.QUEUED, JobState.RUNNING)


def is_live(video: Video) -> bool:
    return video.live_status in LIVE_STATUSES


async def start(session: AsyncSession, video: Video, now: datetime) -> None:
    """Start the watch of a live video that just turned complete. A video
    already watched keeps its watch. The caller commits."""
    if video.state is not VideoState.COMPLETE or not is_live(video):
        return
    await session.execute(
        insert(FormatWatch)
        .values(video_id=video.id, started_at=now)
        .on_conflict_do_nothing(index_elements=["video_id"])
    )


def media_path(archive_root: Path, video: Video) -> Path | None:
    """The stored media file, `<video dir>/<video id>.<ext>`."""
    ext = (video.info or {}).get("ext")
    if not video.dir_path or not ext:
        return None
    return archive_root / video.dir_path / f"{video.id}.{ext}"


def probe_duration(path: Path, tools: Tools) -> float | None:
    """The file's duration from ffprobe, None without ffprobe or a duration."""
    probed = tools.probe(path)
    found = ((probed or {}).get("format") or {}).get("duration")
    return float(found) if found is not None else None


async def replace_active(session: AsyncSession, video_id: str) -> Job | None:
    return await session.scalar(
        select(Job)
        .where(Job.video_id == video_id, Job.kind == JobKind.REPLACE, Job.state.in_(_ACTIVE))
        .limit(1)
    )


def ends_at(watch: FormatWatch) -> datetime:
    return watch.started_at + WATCH_DAYS


async def watch_formats(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    *,
    probe: Probe,
    tools: Tools,
    now: datetime | None = None,
) -> None:
    """End the watches past WATCH_DAYS, then probe each watch that is due."""
    now = now or datetime.now(UTC)
    await end_watches(sessionmaker, settings, now)
    async with sessionmaker() as session:
        due = list(
            await session.scalars(
                select(FormatWatch.video_id)
                .join(Video, Video.id == FormatWatch.video_id)
                .where(
                    FormatWatch.ended_at.is_(None),
                    FormatWatch.started_at > now - WATCH_DAYS,
                    Video.state == VideoState.COMPLETE,
                    FormatWatch.checked_at.is_(None)
                    | (FormatWatch.checked_at <= now - PROBE_EVERY),
                )
                .order_by(FormatWatch.checked_at.nulls_first(), FormatWatch.video_id)
            )
        )
    # One at a time: every probe counts against YouTube's request limits.
    for video_id in due:
        with logs.context(video=video_id):
            try:
                await check(sessionmaker, settings, video_id, probe=probe, tools=tools, now=now)
            except Exception as e:
                emit(
                    log,
                    logging.ERROR,
                    "watch.failed",
                    video=video_id,
                    reason=logs.cut(f"{type(e).__name__}: {e}"),
                    exc_info=True,
                )


async def check(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    video_id: str,
    *,
    probe: Probe,
    tools: Tools,
    now: datetime,
) -> Job | None:
    """Probe one watched video and store what it found. Returns the replace
    job it queued, if any."""
    async with sessionmaker() as session:
        video = await session.get(Video, video_id)
        if video.type is VideoType.MEMBERS:
            profile = await cookies.member_profile(
                session, video.channel_id, cookies.profiles(settings)
            )
        else:
            profile = await cookies.general(session)
    with tempfile.TemporaryDirectory(prefix="savetube-") as private:
        cookiefile = cookies.cookiefile(settings, profile, Path(private))
        try:
            info = await asyncio.to_thread(probe, video_id, cookiefile)
        except Exception as e:
            info, error = None, logs.cut(f"{type(e).__name__}: {e}")
    stored_duration = None
    media = media_path(settings.archive_root, video)
    async with sessionmaker() as session:
        watch = await session.get(FormatWatch, video_id)
        watch.checked_at = now
        if info is None:
            watch.probe_error = error
            await session.commit()
            emit(log, logging.WARNING, "watch.probe_failed", video=video_id, reason=error)
            return None
        if watch.stored_duration is None and media is not None:
            try:
                stored_duration = await asyncio.to_thread(probe_duration, media, tools)
            except IntegrityFailed as e:
                emit(log, logging.WARNING, "watch.ffprobe_failed", video=video_id, reason=str(e))
            watch.stored_duration = stored_duration
        watch.probe_error = None
        watch.formats = formats.counted(info.get("formats") or [])
        watch.vod_duration = float(info["duration"]) if info.get("duration") else None
        job = await _queue(session, watch, video)
        await session.commit()
    picked = formats.picks(watch.formats, video.info or {})
    emit(
        log,
        logging.INFO,
        "watch.probed",
        video=video_id,
        formats=len(watch.formats),
        audio=picked["audio"].format and picked["audio"].format["format_id"],
        video_pick=picked["video"].format and picked["video"].format["format_id"],
        diff=_diff(watch),
        queued=job.format_id if job is not None else None,
    )
    return job


def _diff(watch: FormatWatch) -> float | None:
    if watch.vod_duration is None or watch.stored_duration is None:
        return None
    return round(watch.vod_duration - watch.stored_duration, 1)


async def _queue(session: AsyncSession, watch: FormatWatch, video: Video) -> Job | None:
    """Queue a replace job for the streams whose pick ranks above the stored
    one, unless the user picked by hand, the durations differ, or a replace
    is already queued or running."""
    if watch.manual or await replace_active(session, video.id) is not None:
        return None
    up = [p for p in formats.picks(watch.formats, video.info or {}).values() if p.above]
    if not up:
        return None
    if formats.held(watch.vod_duration, watch.stored_duration):
        emit(log, logging.INFO, "watch.held", video=video.id, diff=_diff(watch))
        return None
    job = Job(
        kind=JobKind.REPLACE,
        video_id=video.id,
        format_id="+".join(p.format["format_id"] for p in up),
    )
    session.add(job)
    await session.flush()
    emit(
        log,
        logging.INFO,
        "watch.queued",
        job=job.id,
        video=video.id,
        format=job.format_id,
        reason="; ".join(p.reason for p in up),
    )
    return job


async def end_watches(sessionmaker: async_sessionmaker, settings: Settings, now: datetime) -> None:
    """End every watch past WATCH_DAYS with no replace queued or running, and
    delete the original a replace moved aside."""
    async with sessionmaker() as session:
        over = list(
            await session.scalars(
                select(FormatWatch).where(
                    FormatWatch.ended_at.is_(None), FormatWatch.started_at <= now - WATCH_DAYS
                )
            )
        )
        for watch in over:
            if await replace_active(session, watch.video_id) is not None:
                continue
            if watch.original_path is not None:
                shutil.rmtree(
                    originals_dir(settings.archive_root, watch.video_id), ignore_errors=True
                )
            watch.ended_at = now
            emit(
                log,
                logging.INFO,
                "watch.ended",
                video=watch.video_id,
                original="deleted" if watch.original_path else "none",
            )
        await session.commit()


def originals_dir(archive_root: Path, video_id: str) -> Path:
    return archive_root / ORIGINALS_DIR / video_id
