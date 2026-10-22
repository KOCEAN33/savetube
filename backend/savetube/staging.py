"""Batch moves from staging into the tree (docs/plan/storage.md, Staging).

Archive downloads work on a staging volume, so the archive's disks stay quiet
while bytes trickle in. A finished video waits there as `staged`, and this
moves the waiting videos into the tree together, in one burst of writes."""

import asyncio
import logging
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube.config import Settings
from savetube.db.models import Video, VideoState
from savetube.limits import running
from savetube.logs import emit, human_bytes, human_duration
from savetube.storage import disk_usage

log = logging.getLogger(__name__)

# The staged bytes that move at once, even while a download runs.
MOVE_BATCH_BYTES = 20 * 1024**3
# Below this much free space on staging, everything waiting moves. The largest
# stored video, 7 GB, holds about twice its size on disk while yt-dlp merges.
MIN_FREE_BYTES = 15 * 1024**3
# A smaller batch moves once no download runs and the newest staged video
# waited this long.
MOVE_IDLE = timedelta(hours=1)


def move_reason(
    staged_bytes: int, free_bytes: int, running: int, newest: datetime | None, now: datetime
) -> str | None:
    """Why the staged videos move now, or None while they wait. `newest` is
    when the latest one was staged, None when none waits."""
    if newest is None:
        return None
    if staged_bytes >= MOVE_BATCH_BYTES:
        return "batch"
    if free_bytes < MIN_FREE_BYTES:
        return "space"
    if running == 0 and now - newest >= MOVE_IDLE:
        return "idle"
    return None


def _copy_in(ready: Path, final: Path, incoming_root: Path) -> None:
    """Copy `ready` to `final` on the archive, then delete it. The copy goes
    to a temporary directory in `.incoming/` first, so the tree never holds a
    half-copied video, and the rename is the only step that changes it."""
    tmp = incoming_root / f"{ready.name}.moving"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(ready, tmp)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.rename(tmp, final)
    shutil.rmtree(ready)


async def move_staged(sessionmaker: async_sessionmaker, settings: Settings) -> None:
    """Move every staged video into the tree when move_reason says so. A
    staged video whose place in the tree exists was renamed there by a move
    cut off before its row changed, and becomes complete. One whose files are
    gone from both goes back to pending, so the next poll downloads it."""
    ready_root = settings.ready_root
    if ready_root is None:
        return
    if not ready_root.parent.is_dir():
        # A missing mount would read every staged video as gone.
        emit(log, logging.ERROR, "staging.unmounted", path=str(ready_root.parent))
        return
    async with sessionmaker() as session:
        staged = Video.state == VideoState.STAGED
        size, newest = (
            await session.execute(
                select(
                    func.coalesce(func.sum(Video.size_bytes), 0), func.max(Video.completed_at)
                ).where(staged)
            )
        ).one()
        busy = await running(session)
    free = disk_usage(ready_root.parent)[1]
    reason = move_reason(size, free, busy, newest, datetime.now(UTC))
    if reason is None:
        return

    async with sessionmaker() as session:
        videos = list(
            await session.scalars(select(Video).where(staged).order_by(Video.completed_at))
        )
    emit(
        log, logging.INFO, "staging.move", reason=reason, count=len(videos), size=human_bytes(size)
    )
    started = time.monotonic()
    moved = 0
    for video in videos:
        ready = ready_root / video.id
        final = settings.archive_root / video.dir_path
        try:
            if ready.is_dir() and not final.exists():
                await asyncio.to_thread(_copy_in, ready, final, settings.incoming_root)
            elif not final.exists():
                emit(log, logging.WARNING, "staging.missing", video=video.id, path=video.dir_path)
                async with sessionmaker() as session:
                    (await session.get(Video, video.id)).state = VideoState.PENDING
                    await session.commit()
                continue
            else:
                shutil.rmtree(ready, ignore_errors=True)
            async with sessionmaker() as session:
                (await session.get(Video, video.id)).state = VideoState.COMPLETE
                await session.commit()
        except Exception as e:
            emit(
                log,
                logging.ERROR,
                "staging.move_failed",
                video=video.id,
                reason=f"{type(e).__name__}: {e}",
            )
            continue
        moved += 1
    emit(
        log,
        logging.INFO,
        "staging.moved",
        count=moved,
        size=human_bytes(size),
        took=human_duration(time.monotonic() - started),
    )
