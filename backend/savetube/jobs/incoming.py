"""Deleting partial download directories no job wants."""

import logging
import shutil
import time

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube.config import Settings
from savetube.db.models import Job, JobKind, JobState, Video
from savetube.logs import emit, human_bytes, human_duration
from savetube.storage import dir_size

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")

# A partial directory no queued or running job wants is deleted after this.
ORPHAN_DAYS = 7


async def clean_incoming(sessionmaker: async_sessionmaker, settings: Settings) -> None:
    """Delete partial directories that no queued or running job wants and that
    nothing wrote to for ORPHAN_DAYS, so cancelled or abandoned parts do not
    fill the dataset."""
    roots = [
        r for r in dict.fromkeys([settings.incoming_root, settings.download_root]) if r.is_dir()
    ]
    if not roots:
        return
    active = Job.state.in_([JobState.QUEUED, JobState.RUNNING])
    async with sessionmaker() as session:
        wanted = set(
            await session.scalars(select(Job.video_id).where(active, Job.kind == JobKind.ARCHIVE))
        )
        # A recording works in `.incoming/<job id>/`.
        wanted |= {str(i) for i in await session.scalars(select(Job.id).where(active))}
    cutoff = time.time() - ORPHAN_DAYS * 86400
    deleted = []
    for d in (d for root in roots for d in root.iterdir()):
        if not d.is_dir() or d.name in wanted:
            continue
        newest = max(p.stat().st_mtime for p in [d, *d.rglob("*")])
        if newest >= cutoff:
            continue
        size = dir_size(d)
        shutil.rmtree(d, ignore_errors=True)
        deleted.append(d.name)
        emit(
            log,
            logging.INFO,
            "incoming.deleted",
            dir=d.name,
            size=human_bytes(size),
            idle=human_duration(time.time() - newest),
        )
    if deleted:
        # The pin names formats of parts that are now gone.
        async with sessionmaker() as session:
            await session.execute(
                update(Video).where(Video.id.in_(deleted)).values(resume_format=None)
            )
            await session.commit()
