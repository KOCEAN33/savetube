import asyncio
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    _events,
    _FakeTools,
    _fields,
    _finished,
    _job_rows,
    _part,
    _queue_row,
    _run_steps,
    _sql,
    _sql_rows,
    _Steps,
)

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Job,
    JobKind,
    JobState,
    Video,
    VideoState,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


@pytest.mark.anyio
async def test_reclaim_interrupts_the_jobs_of_a_dead_worker_and_keeps_their_parts(settings):
    from savetube.jobs import reclaim_stalled

    _job_rows(
        settings,
        "UPDATE job SET state = 'running', attempts = 1, started_at = now() WHERE id = 1",
        "UPDATE video SET state = 'downloading' WHERE id = 'v1'",
        "INSERT INTO job (id, kind, state, video_id, attempts, queue_job_id) VALUES"
        " (2, 'archive', 'queued', 'busy1', 0, 701),"
        " (3, 'archive', 'running', 'busy2', 1, 702)",
        "SELECT setval('job_id_seq', 200)",
    )
    incoming = settings.incoming_root / "v1" / ".tmp"
    incoming.mkdir(parents=True)
    (incoming / "v1.f251.webm.part").write_bytes(b"x")
    finished = []

    async def stalled():
        return [(700, 1), (701, 2)]

    async def finish(queue_job_id):
        finished.append(queue_job_id)

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await reclaim_stalled(sessionmaker, settings, stalled=stalled, finish=finish)
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
        video = await session.get(Video, "v1")
    # The stopped run is a row of its own. The job waits for the dispatcher again.
    [history] = [j for j in jobs.values() if j.id not in (1, 2, 3)]
    assert (history.state, history.video_id, history.kind) == ("interrupted", "v1", JobKind.ARCHIVE)
    assert history.log_tail == "worker stopped during the download"
    assert history.attempts == 1
    assert history.started_at is not None and history.finished_at is not None
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    # A dispatched job no worker ran goes back to waiting, with no history row.
    assert (jobs[2].state, jobs[2].queue_job_id) == (JobState.QUEUED, None)
    assert jobs[3].state is JobState.RUNNING
    assert video.state is VideoState.PENDING
    assert (incoming / "v1.f251.webm.part").read_bytes() == b"x"
    assert finished == [700, 701]
    await engine.dispose()


SECOND_RUN_START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


async def _reclaim_and_start_again(settings):
    """The scheduler reclaims job 1 from a live worker, and a second run of it
    starts, pins its own format, and works in the same directory."""
    from savetube.jobs import reclaim_stalled

    async def stalled():
        return [(500, 1)]

    async def finish(queue_job_id):
        pass

    engine = make_engine(settings.sqlalchemy_url)
    await reclaim_stalled(make_sessionmaker(engine), settings, stalled=stalled, finish=finish)
    await engine.dispose()
    _sql(
        settings,
        "UPDATE job SET state = 'running', attempts = 1, queue_job_id = 501,"
        f" started_at = '{SECOND_RUN_START.isoformat()}' WHERE id = 1",
        "UPDATE video SET state = 'downloading', resume_format = '140' WHERE id = 'v1'",
    )


def _assert_left_to_the_second_run(settings, jobs, video, caplog):
    import logging

    # Only the reclaim wrote a history row.
    [history] = [j for j in jobs.values() if j.id != 1]
    assert history.log_tail == "worker stopped during the download"
    owner = jobs[1]
    assert (owner.state, owner.queue_job_id, owner.attempts) == (JobState.RUNNING, 501, 1)
    assert owner.started_at == SECOND_RUN_START and owner.finished_at is None
    assert (video.state, video.resume_format) == (VideoState.DOWNLOADING, "140")
    assert video.dir_path is None
    assert (settings.incoming_root / "v1").is_dir()
    [line] = _events(caplog, "job.disowned")
    fields = _fields(line)
    assert line.levelno == logging.WARNING
    assert (fields["job"], fields["video"]) == (1, "v1")
    assert fields["reason"]
    assert _events(caplog, "job.failed") == []
    assert _events(caplog, "job.done") == []
    assert [_fields(r)["reason"] for r in _events(caplog, "job.interrupted")] == [
        "worker stopped during the download"
    ]


@pytest.mark.anyio
async def test_a_run_whose_job_was_reclaimed_stops_and_leaves_the_directory(
    settings, caplog, monkeypatch
):
    import logging

    from savetube import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "PAUSE_POLL_SECONDS", 0.05)
    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    cancelled = asyncio.Event()

    async def downloading(params, report):
        _part(params)
        await report({"event": "format", "format_id": "251"})
        await _reclaim_and_start_again(settings)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(downloading)), 10)

    assert cancelled.is_set()
    _assert_left_to_the_second_run(settings, jobs, video, caplog)
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048


@pytest.mark.anyio
async def test_a_disowned_run_that_finishes_moves_nothing_into_the_tree(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    finished = _finished()

    async def done_after_reclaim(params, report):
        info = await finished(params, report)
        await _reclaim_and_start_again(settings)
        return info

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(done_after_reclaim)), 10)

    _assert_left_to_the_second_run(settings, jobs, video, caplog)
    assert (settings.incoming_root / "v1" / "v1.webm").read_bytes() == b"mmmmm"
    assert not (settings.archive_root / "A [UC1]").exists()


@pytest.mark.anyio
async def test_a_disowned_run_that_fails_writes_nothing(settings, caplog):
    import logging

    from savetube.jobs import DownloadFailed

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")

    async def fails_after_reclaim(params, report):
        _part(params)
        await _reclaim_and_start_again(settings)
        raise DownloadFailed("ERROR: something broke")

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(fails_after_reclaim)), 10)

    _assert_left_to_the_second_run(settings, jobs, video, caplog)
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048


@pytest.mark.anyio
async def test_a_disowned_run_reports_nothing_onto_the_new_owners_row(settings, caplog):
    import logging

    from savetube.jobs import DownloadFailed

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")

    async def reports_after_reclaim(params, report):
        _part(params)
        await _reclaim_and_start_again(settings)
        _sql(settings, """UPDATE job SET progress = '{"percent": 50.0}' WHERE id = 1""")
        await report({"percent": 10.0})
        await report({"event": "format", "format_id": "251"})
        raise DownloadFailed("ERROR: something broke")

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(
            _run_steps(settings, _Steps(reports_after_reclaim)), 10
        )

    _assert_left_to_the_second_run(settings, jobs, video, caplog)
    assert jobs[1].progress == {"percent": 50.0}


@pytest.mark.anyio
async def test_reclaim_takes_a_worker_as_stalled_after_300_s_without_a_heartbeat(
    settings, monkeypatch
):
    from savetube import scheduler
    from savetube.queue import app

    asked = []

    async def get_stalled_jobs(**kwargs):
        asked.append(kwargs)
        return []

    engine = make_engine(settings.sqlalchemy_url)
    monkeypatch.setattr(scheduler, "default_sessionmaker", lambda: make_sessionmaker(engine))
    monkeypatch.setattr(app.job_manager, "get_stalled_jobs", get_stalled_jobs)
    await scheduler.reclaim(settings)
    await engine.dispose()
    [kwargs] = asked
    assert kwargs["seconds_since_heartbeat"] == 300
    assert (kwargs["queue"], kwargs["task_name"]) == ("archive", "archive")


async def _work(settings, monkeypatch, step, *, reclaim):
    """A real worker takes job 1's queue job. With `reclaim`, the run first
    plays the scheduler's reclaim, which fails that queue job, and a second
    run's start. Returns the queue job's id."""
    from procrastinate import PsycopgConnector
    from procrastinate.jobs import Status

    from savetube import jobs as jobs_module
    from savetube.db import PSYCOPG_CONNECT_ARGS
    from savetube.queue import ARCHIVE_QUEUE, app, archive

    real_run = jobs_module.run
    engine = make_engine(settings.sqlalchemy_url)

    async def first(params, report):
        if reclaim:
            await app.job_manager.finish_job_by_id_async(
                queue_job_id, status=Status.FAILED, delete_job=False
            )
            await _reclaim_and_start_again(settings)
        return await step(params, report)

    async def run(job_id, _settings, *, live, runner):
        await real_run(
            job_id,
            settings,
            live=live,
            runner=_Steps(first),
            sessionmaker=make_sessionmaker(engine),
            tools=_FakeTools(),
        )

    monkeypatch.setattr(jobs_module, "run", run)
    connector = PsycopgConnector(conninfo=settings.conninfo, kwargs=PSYCOPG_CONNECT_ARGS)
    with app.replace_connector(connector):
        async with app.open_async():
            queue_job_id = await archive.defer_async(job_id=1)
            _sql(settings, f"UPDATE job SET queue_job_id = {queue_job_id} WHERE id = 1")
            # The worker settings of savetube.cli.
            await asyncio.wait_for(
                app.run_worker_async(
                    queues=[ARCHIVE_QUEUE],
                    wait=False,
                    listen_notify=False,
                    delete_jobs="successful",
                    install_signal_handlers=False,
                ),
                10,
            )
    await engine.dispose()
    return queue_job_id


def _errors(caplog):
    import logging

    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["done", "failed"])
async def test_a_worker_ends_a_reclaimed_queue_job_without_an_error(
    settings, caplog, monkeypatch, outcome
):
    import gc
    import logging

    from savetube.jobs import DownloadFailed

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    finished = _finished()

    async def step(params, report):
        if outcome == "failed":
            _part(params)
            raise DownloadFailed("ERROR: something broke")
        return await finished(params, report)

    with caplog.at_level(logging.DEBUG):
        queue_job_id = await _work(settings, monkeypatch, step, reclaim=True)
        # An unretrieved task exception logs when the task is collected.
        gc.collect()

    assert _errors(caplog) == []
    assert [_fields(r)["job"] for r in _events(caplog, "job.disowned")] == [1]
    rows = _sql_rows(settings, f"SELECT status FROM procrastinate_jobs WHERE id = {queue_job_id}")
    assert rows == [("failed",)]


@pytest.mark.anyio
async def test_a_worker_still_ends_its_own_queue_job(settings, caplog, monkeypatch):
    import gc
    import logging

    from procrastinate import PsycopgConnector
    from procrastinate.exceptions import ConnectorException
    from procrastinate.jobs import Job as QueueJob
    from procrastinate.jobs import Status

    from savetube.db import PSYCOPG_CONNECT_ARGS
    from savetube.queue import app

    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    with caplog.at_level(logging.DEBUG):
        queue_job_id = await _work(settings, monkeypatch, _finished(), reclaim=False)
        gc.collect()

    assert _errors(caplog) == []
    assert [_fields(r)["job"] for r in _events(caplog, "job.done")] == [1]
    rows = _sql_rows(settings, f"SELECT id FROM procrastinate_jobs WHERE id = {queue_job_id}")
    assert rows == []

    # Only a queue job the reclaim failed counts as already ended.
    ended = _queue_row(settings, "succeeded")
    job = QueueJob(id=ended, queue="archive", lock=None, queueing_lock=None, task_name="archive")
    connector = PsycopgConnector(conninfo=settings.conninfo, kwargs=PSYCOPG_CONNECT_ARGS)
    with app.replace_connector(connector):
        async with app.open_async():
            with pytest.raises(ConnectorException):
                await app.job_manager.finish_job(job, Status.SUCCEEDED, delete_job=True)
