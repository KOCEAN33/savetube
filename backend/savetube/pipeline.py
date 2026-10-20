"""What archive downloads and live recordings share: from a finished download in
`.incoming/<job id>/` to a video directory in the tree and a `video` row."""

import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube.db.models import (
    Channel,
    Job,
    JobState,
    Source,
    Video,
    VideoSource,
    VideoState,
)
from savetube.layout import channel_dir_name, classify, video_dir
from savetube.storage import dir_size, finalize


def prepare_incoming(incoming_root: Path, job_id: int) -> Path:
    """An empty working directory. A retried recording starts again from the
    first segment, so whatever a dead attempt left is discarded."""
    path = incoming_root / str(job_id)
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


async def settings_channel(session: AsyncSession, video_id: str) -> Channel | None:
    """The channel of a source referencing the video, preferring one that uses
    cookies. Its format and cookie use apply to the download. A detected live
    no source lists yet takes its uploader's channel."""
    q = (
        select(Channel)
        .join(Source, Source.channel_id == Channel.id)
        .join(VideoSource, VideoSource.source_id == Source.id)
        .where(VideoSource.video_id == video_id)
        .order_by(Channel.use_cookies.desc(), Source.id)
        .limit(1)
    )
    return await session.scalar(q) or await session.scalar(
        select(Channel).join(Video, Video.channel_id == Channel.id).where(Video.id == video_id)
    )


async def from_shorts_tab(session: AsyncSession, video_id: str) -> bool:
    q = (
        select(Source.id)
        .join(VideoSource, VideoSource.source_id == Source.id)
        .where(VideoSource.video_id == video_id, Source.url.like("%/shorts"))
        .limit(1)
    )
    return await session.scalar(q) is not None


async def get_or_create_channel(session: AsyncSession, info: dict[str, Any]) -> Channel:
    """`info` is a video's info dict, or a flat listing with the same channel keys."""
    channel_id = info["channel_id"]
    channel = await session.get(Channel, channel_id)
    if channel is None:
        name = info.get("channel") or info.get("uploader") or channel_id
        channel = Channel(
            id=channel_id,
            handle=info.get("uploader_id"),
            name=name,
            dir_name=channel_dir_name(name, channel_id),
        )
        session.add(channel)
    return channel


async def store(
    session: AsyncSession,
    *,
    job: Job,
    info: dict[str, Any],
    incoming: Path,
    archive_root: Path,
    from_shorts_tab: bool = False,
) -> Video:
    """Move the download into the tree and record it. The caller commits."""
    channel = await get_or_create_channel(session, info)
    video_type = classify(info, from_shorts_tab=from_shorts_tab)
    rel = video_dir(channel.dir_name, video_type, info)
    final = archive_root / rel
    finalize(incoming, final)

    video = await session.get(Video, info["id"]) or Video(id=info["id"])
    session.add(video)
    upload_date = info.get("upload_date")
    video.channel_id = channel.id
    video.type = video_type
    video.title = info.get("title") or info["id"]
    video.upload_date = date.fromisoformat(upload_date) if upload_date else None
    video.duration = int(info["duration"]) if info.get("duration") else None
    video.live_status = info.get("live_status")
    video.availability = info.get("availability")
    video.format_id = info.get("format_id")
    video.dir_path = rel.as_posix()
    video.size_bytes = dir_size(final)
    video.completed_at = datetime.now(UTC)
    video.state = VideoState.COMPLETE
    video.info = info

    job.state = JobState.SUCCEEDED
    job.finished_at = video.completed_at
    return video
