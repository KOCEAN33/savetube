"""Hourly database cleanup: stale progress, finished queue jobs, the
interrupted runs of videos archived long ago, and live detection's probed ids
no listing showed for long."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, column, delete, func, null, select, table, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube.db.models import Job, JobKind, JobState, LiveSeen
from savetube.logs import emit

log = logging.getLogger(__name__)

# A finished procrastinate job is kept this long after its last event.
QUEUE_JOB_DAYS = 30
# An archive job's interrupted rows are kept this long after the video's
# latest successful download.
INTERRUPTED_DAYS = 30
# A `live_seen` row is kept this long after a check last listed its id. Every
# check touches the ids it lists, so an id still listed is never deleted.
LIVE_SEEN_DAYS = 30

# The procrastinate statuses no worker or dispatch reads again. `aborting` is
# legacy and unused since procrastinate 3.0.
FINISHED_QUEUE_STATUSES = ["succeeded", "failed", "cancelled", "aborted"]

_queue_jobs = table("procrastinate_jobs", column("id"), column("status"))
_queue_events = table("procrastinate_events", column("job_id"), column("at"))


async def clean_database(sessionmaker: async_sessionmaker) -> None:
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        progress = await session.execute(
            update(Job)
            .where(Job.state != JobState.RUNNING, Job.progress.is_not(None))
            .values(progress=null())
        )
        # Mirrors procrastinate's `delete_old_jobs`, which returns no count. A
        # job's events go with it (ON DELETE CASCADE). A dispatched `job` row
        # whose queue job is gone goes back to waiting (savetube.jobs.dispatch).
        last_event = (
            select(func.max(_queue_events.c.at))
            .where(_queue_events.c.job_id == _queue_jobs.c.id)
            .scalar_subquery()
        )
        queue_jobs = await session.execute(
            delete(_queue_jobs).where(
                _queue_jobs.c.status.cast(Text).in_(FINISHED_QUEUE_STATUSES),
                last_event < now - timedelta(days=QUEUE_JOB_DAYS),
            )
        )
        archive = Job.kind == JobKind.ARCHIVE
        archived_long_ago = (
            select(Job.video_id)
            .where(archive, Job.state == JobState.SUCCEEDED)
            .group_by(Job.video_id)
            .having(func.max(Job.finished_at) < now - timedelta(days=INTERRUPTED_DAYS))
        )
        interrupted = await session.execute(
            delete(Job).where(
                archive,
                Job.state == JobState.INTERRUPTED,
                Job.video_id.in_(archived_long_ago),
            )
        )
        live_seen = await session.execute(
            delete(LiveSeen).where(LiveSeen.listed_at < now - timedelta(days=LIVE_SEEN_DAYS))
        )
        await session.commit()
    counts = {
        "progress": progress.rowcount,
        "queue_jobs": queue_jobs.rowcount,
        "interrupted": interrupted.rowcount,
        "live_seen": live_seen.rowcount,
    }
    if any(counts.values()):
        emit(log, logging.INFO, "cleanup.done", **counts)
