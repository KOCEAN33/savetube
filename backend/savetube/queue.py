"""The archive queue: procrastinate on the same Postgres database. No Redis."""

from procrastinate import App, PsycopgConnector
from procrastinate.exceptions import ConnectorException
from procrastinate.jobs import Job, Status
from procrastinate.manager import JobManager

from savetube import jobs
from savetube.config import get_settings
from savetube.db import PSYCOPG_CONNECT_ARGS

ARCHIVE_QUEUE = "archive"


class _JobManager(JobManager):
    async def finish_job(self, job: Job, status: Status, delete_job: bool) -> None:
        """The worker's end of a queue job. The reclaim fails the queue job of a
        run it took, and that run logs `job.disowned` and ends, so a failed
        queue job is already final."""
        try:
            await super().finish_job(job, status, delete_job)
        except ConnectorException:
            rows = await self.connector.execute_query_all_async(
                "SELECT status FROM procrastinate_jobs WHERE id = %(id)s", id=job.id
            )
            if rows != [{"status": Status.FAILED.value}]:
                raise


app = App(connector=PsycopgConnector(conninfo=get_settings().conninfo, kwargs=PSYCOPG_CONNECT_ARGS))
# Before any task, which keeps the manager it was made with.
app.job_manager = _JobManager(app.connector)


async def enqueue(job_id: int) -> int:
    return await archive.defer_async(job_id=job_id)


# Retries belong to the `job` row and the scheduler, so a failure is final here.
# A replace job runs through this task too (savetube.jobs.run), so the
# stalled-job reclaim, which reads one task name, covers it.
@app.task(name="archive", queue=ARCHIVE_QUEUE, retry=False)
async def archive(job_id: int) -> None:
    try:
        await jobs.run(job_id, get_settings(), live=False, runner=jobs.in_child)
    except Exception as e:
        # The job row and the `job.failed` line hold it. Raising would make
        # procrastinate log a second error with the same traceback.
        if not jobs.recorded(e):
            raise
