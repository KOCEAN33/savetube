"""The archive queue: procrastinate on the same Postgres database. No Redis."""

from procrastinate import App, PsycopgConnector

from savetube import jobs
from savetube.config import get_settings
from savetube.db import PSYCOPG_CONNECT_ARGS

ARCHIVE_QUEUE = "archive"

app = App(connector=PsycopgConnector(conninfo=get_settings().conninfo, kwargs=PSYCOPG_CONNECT_ARGS))


# Retries belong to the `job` row and the scheduler, so a failure is final here.
@app.task(name="archive", queue=ARCHIVE_QUEUE, retry=False)
async def archive(job_id: int) -> None:
    await jobs.run(job_id, get_settings(), live=False, runner=jobs.in_child)
