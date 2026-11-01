"""Deleting a stored video at the user's ask (docs/plan/storage.md, Deleting
a video). Its files go, and its row stays as `deleted`, so no poll, live
check, format watch or re-encoding takes it up again."""

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube.config import Settings
from savetube.db.models import (
    EncodeJob,
    EncodeState,
    FormatWatch,
    Job,
    JobState,
    Video,
    VideoState,
)
from savetube.logs import emit, human_bytes
from savetube.storage import dir_size
from savetube.watch import originals_dir

log = logging.getLogger(__name__)

_DELETABLE = (VideoState.COMPLETE, VideoState.STAGED)


class NotDeletable(Exception):
    """The video has no stored files to delete, or something writes them now."""


def _inside(root: Path, path: Path) -> Path:
    """`path` resolved, raising NotDeletable unless it lies below `root`."""
    root, resolved = root.resolve(), path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise NotDeletable(f"{path} is outside {root}")
    return resolved


def _targets(settings: Settings, video: Video) -> list[Path]:
    """The video's directory in the tree, its kept original and its staged copy."""
    root = settings.archive_root
    found = [_inside(root, originals_dir(root, video.id))]
    if video.dir_path:
        found.append(_inside(root, root / video.dir_path))
    if video.state is VideoState.STAGED and settings.ready_root is not None:
        found.append(_inside(settings.ready_root, settings.ready_root / video.id))
    return found


def _remove(paths: list[Path]) -> int:
    """Delete each directory that exists. Return the bytes its files held."""
    freed = 0
    for path in paths:
        if path.is_dir():
            freed += dir_size(path)
            shutil.rmtree(path)
    return freed


async def delete(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    video_id: str,
    *,
    probes: asyncio.Lock,
    encodes: asyncio.Lock,
    moves: asyncio.Lock,
) -> int:
    """Delete the video's directory in the tree, its kept original and its
    staged copy, then mark it deleted. Return the bytes freed. Raises
    LookupError for an unknown video and NotDeletable for one that is not
    stored or that a job, an encode or a staging move holds.

    `probes` and `encodes` keep the format watch and the encode loop from
    queueing work for the video between the check and the delete. A staged
    video is refused during a staging move, which can last an hour."""
    async with probes, encodes, sessionmaker() as session:
        video = await session.get(Video, video_id, with_for_update=True)
        if video is None:
            raise LookupError(video_id)
        if video.state not in _DELETABLE:
            raise NotDeletable(f"the video is {video.state.value}")
        job = await session.scalar(
            select(Job)
            .where(Job.video_id == video_id, Job.state.in_([JobState.QUEUED, JobState.RUNNING]))
            .limit(1)
        )
        if job is not None:
            raise NotDeletable(f"{job.kind.value} job {job.id} is {job.state.value}")
        encode = await session.scalar(
            select(EncodeJob)
            .where(
                EncodeJob.video_id == video_id,
                EncodeJob.state.in_([EncodeState.QUEUED, EncodeState.CLAIMED]),
            )
            .limit(1)
        )
        if encode is not None:
            raise NotDeletable(f"encode job {encode.id} is {encode.state.value}")
        async with contextlib.AsyncExitStack() as stack:
            staged = video.state is VideoState.STAGED
            if staged:
                if moves.locked():
                    raise NotDeletable("a staging move is running")
                await stack.enter_async_context(moves)
            freed = await asyncio.to_thread(_remove, _targets(settings, video))
            # Files first: a failed removal leaves the row stored, so a retry
            # finds what is left. The row lock holds until this commit.
            video.state = VideoState.DELETED
            video.dir_path = None
            video.size_bytes = None
            video.completed_at = None
            video.verified_at = None
            video.verified_checks = None
            video.resume_format = None
            await session.execute(
                update(FormatWatch)
                .where(FormatWatch.video_id == video_id)
                .values(
                    ended_at=func.coalesce(FormatWatch.ended_at, func.now()),
                    original_path=None,
                    original_until=None,
                )
            )
            await session.commit()
    emit(log, logging.INFO, "video.deleted", video=video_id, size=human_bytes(freed))
    return freed
