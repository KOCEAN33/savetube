import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    _api,
    _block_state,
    _dispatch,
    _events,
    _fields,
    _finished,
    _job_rows,
    _part,
    _run,
    _run_steps,
    _sql,
    _sql_rows,
    _Steps,
    _waiting_rows,
)

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Job,
    JobState,
    VideoState,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


BOT_CHECK = (
    "DownloadError: ERROR: [youtube] v1: Sign in to confirm you’re not a bot."
    " Use --cookies-from-browser or --cookies for the authentication."
)


async def _blocked_step(params, report):
    from savetube.jobs import DownloadFailed

    _part(params)
    await report({"event": "format", "format_id": "251"})
    raise DownloadFailed(BOT_CHECK)


@pytest.mark.anyio
async def test_a_youtube_block_interrupts_the_archive_job_and_pauses_downloads(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    before = datetime.now(UTC)
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(_blocked_step))
    [history] = [j for j in jobs.values() if j.id != 1]
    assert (history.state, history.video_id) == ("interrupted", "v1")
    assert history.log_tail == f"blocked by YouTube: {BOT_CHECK}"
    # No attempt counts, and the parts and the pin stay for the resume.
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert video.state is VideoState.PENDING and video.resume_format == "251"
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048
    count, until = _block_state(settings)
    assert count == 1
    assert before + timedelta(minutes=30) <= until <= datetime.now(UTC) + timedelta(minutes=30)
    assert _events(caplog, "job.failed") == []
    [line] = _events(caplog, "job.interrupted")
    assert line.levelno == logging.WARNING and _fields(line)["reason"] == "blocked by YouTube"
    fields = _fields(line)
    assert list(fields) == ["job", "video", "channel", "title", "took", "present", "reason"]
    assert fields["present"] == "2KiB"
    [blocked] = _events(caplog, "dispatch.blocked")
    assert blocked.levelno == logging.WARNING
    fields = _fields(blocked)
    assert (fields["count"], fields["backoff"], fields["job"], fields["video"]) == (
        1,
        "30m0s",
        1,
        "v1",
    )


@pytest.mark.anyio
async def test_a_block_while_paused_interrupts_without_lengthening_the_pause(settings, caplog):
    import logging

    _job_rows(settings, "SELECT setval('job_id_seq', 200)")

    async def blocked_together(params, report):
        from savetube.jobs import DownloadFailed

        # Another job hit the block while this one was downloading.
        _sql(
            settings,
            "UPDATE download_settings SET block_count = 1,"
            " blocked_until = now() + interval '30 minutes'",
        )
        raise DownloadFailed("ERROR: unable to download video data: HTTP Error 429")

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _Steps(blocked_together))
    assert jobs[1].state is JobState.QUEUED and jobs[1].attempts == 0
    count, until = _block_state(settings)
    assert count == 1 and until < datetime.now(UTC) + timedelta(minutes=30)
    assert _events(caplog, "dispatch.blocked") == []
    assert len(_events(caplog, "job.interrupted")) == 1


@pytest.mark.anyio
async def test_dispatch_sends_nothing_while_blocked_then_one_probe(settings):
    _waiting_rows(
        settings,
        "UPDATE job SET state = 'succeeded' WHERE id = 10",
        "UPDATE download_settings SET max_concurrent = 5, block_count = 2,"
        " blocked_until = '2026-10-21T03:00:00Z'",
    )
    until = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
    enqueued, jobs = await _dispatch(settings, until - timedelta(minutes=1))
    assert enqueued == []
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [until, until, until]

    # The pause is over, but until a download succeeds only one runs at a time.
    enqueued, jobs = await _dispatch(settings, until)
    assert enqueued == [12]
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [None, None, None]


@pytest.mark.anyio
async def test_dispatch_while_blocked_waits_for_the_later_of_pause_and_window(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5, block_count = 1,"
        " window_start = '23:00', window_end = '05:00', window_enabled = true,"
        " blocked_until = '2026-10-22T00:30:00+09'",
    )
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, noon)
    assert enqueued == []
    assert jobs[12].deferred_until == datetime(2026, 10, 22, 0, 30, tzinfo=TIMEZONE)


@pytest.mark.anyio
async def test_a_worker_hands_back_a_job_while_blocked(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE download_settings SET blocked_until = now() + interval '1 hour'")
    with caplog.at_level(logging.INFO, logger="savetube"):
        job, runner, _ = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.queue_job_id is None
    [line] = _events(caplog, "job.returned")
    assert _fields(line)["reason"] == "blocked"


@pytest.mark.anyio
async def test_the_probe_admits_one_archive_job_at_a_time(settings, caplog):
    import logging

    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1)",
        "UPDATE download_settings SET max_concurrent = 5, block_count = 1,"
        " blocked_until = now() - interval '1 minute'",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        job, runner, _ = await _run(settings)
    assert runner.params is None and job.state is JobState.QUEUED
    assert _fields(_events(caplog, "job.returned")[0])["reason"] == "slots"


@pytest.mark.anyio
async def test_a_success_after_the_pause_ends_the_probe(settings, caplog):
    import logging

    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa'",
        "UPDATE download_settings SET block_count = 2, blocked_until = now() - interval '1 minute'",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _Steps(_finished()))
    assert jobs[1].state is JobState.SUCCEEDED
    assert _block_state(settings) == (0, None)
    [line] = _events(caplog, "dispatch.unblocked")
    assert line.levelno == logging.INFO and _fields(line)["count"] == 2


@pytest.mark.anyio
async def test_a_blocked_recording_still_fails(settings, caplog):
    import logging

    from savetube.jobs import DownloadFailed, run

    _job_rows(settings, "UPDATE job SET kind = 'record'")

    async def blocked(params, report):
        raise DownloadFailed(BOT_CHECK)

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    with caplog.at_level(logging.INFO, logger="savetube"), pytest.raises(DownloadFailed):
        await run(1, settings, live=True, runner=_Steps(blocked), sessionmaker=sessionmaker)
    async with sessionmaker() as session:
        job = await session.get(Job, 1)
    await engine.dispose()
    assert job.state is JobState.FAILED and job.log_tail == BOT_CHECK
    assert _block_state(settings) == (0, None)
    assert len(_events(caplog, "job.failed")) == 1


def test_migration_0009_gives_back_attempts_a_bot_check_took(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0008")
    _job_rows(
        settings,
        "UPDATE job SET state = 'succeeded' WHERE id = 1",
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail) VALUES"
        " (2, 'archive', 'failed', 'v1', 1, 'Sign in to confirm you’re not a bot.'),"
        " (3, 'archive', 'failed', 'v1', 1, 'HTTP Error 403: Forbidden'),"
        " (4, 'record', 'failed', 'busy2', 1, 'Sign in to confirm you’re NOT A BOT.')",
    )
    command.upgrade(cfg, "head")
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        states = dict(c.execute(sa.text("SELECT id, state FROM job")).all())
    assert states == {1: "succeeded", 2: "interrupted", 3: "failed", 4: "failed"}
    assert _block_state(settings) == (0, None)

    command.downgrade(cfg, "0008")
    with engine.connect() as c:
        states = dict(c.execute(sa.text("SELECT id, state FROM job")).all())
        columns = set(
            c.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'download_settings'"
                )
            ).scalars()
        )
    engine.dispose()
    # The downgrade leaves the data as it is.
    assert states[2] == "interrupted"
    assert not columns & {"blocked_until", "block_count"}


def test_migration_0012_sets_failed_videos_with_a_queued_archive_job_pending(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0011")
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('waits', 'UC1', 'videos', 't', 'failed'), ('gave_up', 'UC1', 'videos', 't', 'failed'),"
        " ('recorded', 'UC1', 'lives', 't', 'failed'), ('done', 'UC1', 'videos', 't', 'complete')",
        "INSERT INTO job (kind, state, video_id, attempts) VALUES"
        " ('archive', 'failed', 'waits', 1), ('archive', 'queued', 'waits', 0),"
        " ('archive', 'failed', 'gave_up', 1),"
        " ('record', 'queued', 'recorded', 0),"
        " ('archive', 'queued', 'done', 0)",
    )
    command.upgrade(cfg, "head")
    states = dict(_sql_rows(settings, "SELECT id, state FROM video"))
    assert states == {
        "waits": "pending",
        "gave_up": "failed",
        "recorded": "failed",
        "done": "complete",
    }
    command.downgrade(cfg, "0011")
    assert dict(_sql_rows(settings, "SELECT id, state FROM video"))["waits"] == "pending"


@pytest.mark.anyio
async def test_dispatch_sends_nothing_while_paused_and_leaves_no_deferral(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE job SET state = 'succeeded' WHERE id = 10",
        "UPDATE download_settings SET max_concurrent = 5, paused = true,"
        " window_start = '23:00', window_end = '05:00', window_enabled = true",
    )
    # Outside the window too, but a pause has no known end.
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, noon)
    assert enqueued == []
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [None, None, None]
    opens = datetime(2026, 10, 21, 23, 0, tzinfo=TIMEZONE)
    enqueued, _ = await _dispatch(settings, opens)
    assert enqueued == []

    _sql(settings, "UPDATE download_settings SET paused = false")
    enqueued, _ = await _dispatch(settings, opens)
    assert enqueued == [12, 13, 14]


@pytest.mark.anyio
async def test_a_worker_hands_back_a_job_while_paused(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE download_settings SET paused = true")
    with caplog.at_level(logging.INFO, logger="savetube"):
        job, runner, _ = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.queue_job_id is None and job.attempts == 0
    assert job.deferred_until is None
    [line] = _events(caplog, "job.returned")
    assert _fields(line)["reason"] == "paused"


@pytest.mark.anyio
async def test_a_pause_interrupts_a_running_archive_download_and_keeps_its_parts(
    settings, caplog, monkeypatch
):
    import logging

    from savetube.jobs import guards

    monkeypatch.setattr(guards, "PAUSE_POLL_SECONDS", 0.05)
    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")

    async def downloading(params, report):
        _part(params)
        await report({"event": "format", "format_id": "251"})
        _sql(settings, "UPDATE download_settings SET paused = true")
        await asyncio.Event().wait()

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(downloading)), 10)

    [history] = [j for j in jobs.values() if j.id != 1]
    assert (history.state, history.video_id) == ("interrupted", "v1")
    assert history.log_tail == "paused by user"
    assert history.attempts == 1 and history.finished_at is not None
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert jobs[1].deferred_until is None
    assert video.state is VideoState.PENDING and video.resume_format == "251"
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048
    assert _events(caplog, "job.failed") == []
    [line] = _events(caplog, "job.interrupted")
    assert line.levelno == logging.INFO and _fields(line)["reason"] == "paused by user"


@pytest.mark.anyio
async def test_a_pause_never_cancels_a_live_recording(settings, monkeypatch):
    from savetube.jobs import DownloadFailed, guards, run

    monkeypatch.setattr(guards, "PAUSE_POLL_SECONDS", 0.01)
    _job_rows(
        settings,
        "UPDATE job SET kind = 'record'",
        "UPDATE download_settings SET paused = true",
    )

    async def recording(params, report):
        # Many poll intervals pass while the recording runs.
        await asyncio.sleep(0.3)
        raise DownloadFailed("the live ended badly")

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    with pytest.raises(DownloadFailed):
        await run(1, settings, live=True, runner=_Steps(recording), sessionmaker=sessionmaker)
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
    await engine.dispose()
    assert list(jobs) == [1]
    assert jobs[1].state is JobState.FAILED and jobs[1].log_tail == "the live ended badly"


def test_api_pauses_and_resumes_archive_downloads(settings, caplog):
    import logging

    _job_rows(settings)
    with _api(settings) as client, caplog.at_level(logging.INFO, logger="savetube"):
        assert client.get("/api/download-settings").json()["paused"] is False
        put = client.put("/api/download-settings/paused", json={"paused": True})
        assert put.status_code == 200 and put.json()["paused"] is True
        assert client.get("/api/download-settings").json()["paused"] is True
        # Saving the limits leaves the pause as it is.
        limits = {"max_concurrent": 2, "cookie_profile": None}
        assert client.put("/api/download-settings", json=limits).json()["paused"] is True
        assert client.put("/api/download-settings/paused", json={}).status_code == 422
        put = client.put("/api/download-settings/paused", json={"paused": False})
        assert put.json()["paused"] is False
    [paused] = _events(caplog, "dispatch.paused")
    [resumed] = _events(caplog, "dispatch.resumed")
    assert paused.levelno == resumed.levelno == logging.INFO
