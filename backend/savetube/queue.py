"""The archive queue: procrastinate on the same Postgres database. No Redis."""

from procrastinate import App, PsycopgConnector

from savetube import jobs
from savetube.config import get_settings
from savetube.db import PSYCOPG_CONNECT_ARGS

ARCHIVE_QUEUE = "archive"

app = App(connector=PsycopgConnector(conninfo=get_settings().conninfo, kwargs=PSYCOPG_CONNECT_ARGS))


async def enqueue(job_id: int) -> int:
    return await archive.defer_async(job_id=job_id)


# Retries belong to the `job` row and the scheduler, so a failure is final here.
@app.task(name="archive", queue=ARCHIVE_QUEUE, retry=False)
async def archive(job_id: int) -> None:
    try:
        await jobs.run(job_id, get_settings(), live=False, runner=jobs.in_child)
    except Exception as e:
        # The job row and the `job.failed` line hold it. Raising would make
        # procrastinate log a second error with the same traceback.
        if not jobs.recorded(e):
            raise
