"""The archive queue: procrastinate on the same Postgres database. No Redis."""

from datetime import datetime

from procrastinate import App, PsycopgConnector

from savetube import jobs
from savetube.config import get_settings
from savetube.db import PSYCOPG_CONNECT_ARGS

ARCHIVE_QUEUE = "archive"

app = App(connector=PsycopgConnector(conninfo=get_settings().conninfo, kwargs=PSYCOPG_CONNECT_ARGS))


async def _defer(job_id: int, at: datetime) -> int:
    return await archive.configure(schedule_at=at).defer_async(job_id=job_id)


async def requeue_now(job_id: int, queue_job_id: int | None) -> int | None:
    """Replace a deferred queue job with one due now. None when a worker took
    the old one first, so it runs as it is."""
    if queue_job_id is not None and not await app.job_manager.cancel_job_by_id_async(queue_job_id):
        return None
    return await archive.defer_async(job_id=job_id)


# Retries belong to the `job` row and the scheduler, so a failure is final here.
@app.task(name="archive", queue=ARCHIVE_QUEUE, retry=False)
async def archive(job_id: int) -> None:
    await jobs.run(job_id, get_settings(), live=False, runner=jobs.in_child, defer=_defer)
