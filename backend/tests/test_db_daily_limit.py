import asyncio
from datetime import datetime, timedelta
from datetime import time as dt_time

import pytest
from conftest import (
    ADMIN_URL,
    _api,
    _closed_window_sql,
    _dispatch,
    _events,
    _fields,
    _finished,
    _job_rows,
    _run,
    _run_steps,
    _sql,
    _sql_rows,
    _Steps,
    _waiting_rows,
)

from savetube.db.models import (
    JobState,
    VideoState,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


# The start of today in Seoul, as SQL.
_KST_TODAY = "(date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul')"


@pytest.mark.anyio
async def test_the_daily_limit_hands_back_a_4th_start_at_limit_3(settings, caplog):
    import logging

    _job_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 10, daily_limit = 3",
        # Two successes today and one running download make three.
        "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
        " (2, 'archive', 'succeeded', 'busy1', 1, now()),"
        f" (3, 'archive', 'succeeded', 'busy1', 1, {_KST_TODAY}),"
        " (4, 'archive', 'running', 'busy1', 1, NULL),"
        # None of these count: yesterday, a recording, an interrupted run, a failure.
        f" (5, 'archive', 'succeeded', 'busy1', 1, {_KST_TODAY} - interval '1 second'),"
        " (6, 'record', 'succeeded', 'busy2', 1, now()),"
        " (7, 'archive', 'interrupted', 'busy1', 1, now()),"
        " (8, 'archive', 'failed', 'busy1', 1, now())",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        job, runner, queue_rows = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.queue_job_id is None and job.attempts == 0
    assert queue_rows == 0
    [line] = _events(caplog, "job.returned")
    assert _fields(line)["reason"] == "daily_limit"

    _sql(settings, "UPDATE download_settings SET daily_limit = 4")
    job, runner, _ = await _run(settings)
    assert runner.params is not None and job.attempts == 1


@pytest.mark.anyio
async def test_a_download_running_when_the_daily_limit_is_reached_runs_to_the_end(
    settings, monkeypatch
):
    from savetube import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "PAUSE_POLL_SECONDS", 0.05)
    _job_rows(settings, "UPDATE channel SET format = 'wa'")

    async def capped_mid_run(params, report):
        _sql(
            settings,
            "UPDATE download_settings SET daily_limit = 1",
            "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
            " (2, 'archive', 'succeeded', 'busy1', 1, now())",
        )
        await asyncio.sleep(0.3)
        return await _finished()(params, report)

    jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(capped_mid_run)), 10)
    assert jobs[1].state is JobState.SUCCEEDED
    assert video.state is VideoState.COMPLETE


@pytest.mark.anyio
async def test_dispatch_fills_only_the_slots_the_daily_limit_leaves(settings):
    from savetube.limits import TIMEZONE

    now = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5, daily_limit = 3",
        # Job 10 runs, so one success today leaves one start.
        "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
        " (20, 'archive', 'succeeded', 'a', 1, '2026-10-21T08:00:00+09')",
    )
    enqueued, jobs = await _dispatch(settings, now)
    assert enqueued == [12]
    assert [jobs[i].deferred_until for i in (13, 14)] == [None, None]
    # The dispatched job holds the last start of the day.
    enqueued, _ = await _dispatch(settings, now)
    assert enqueued == []


@pytest.mark.anyio
async def test_the_daily_count_resets_at_midnight_in_seoul(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE job SET state = 'succeeded', finished_at = '2026-10-21T23:30:00+09' WHERE id = 10",
        "UPDATE download_settings SET max_concurrent = 5, daily_limit = 1",
    )
    midnight = datetime(2026, 10, 22, 0, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, midnight - timedelta(minutes=10))
    assert enqueued == []
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [midnight, midnight, midnight]
    assert jobs[15].deferred_until is None

    enqueued, jobs = await _dispatch(settings, midnight)
    assert enqueued == [12]
    assert [jobs[i].deferred_until for i in (13, 14)] == [None, None]


@pytest.mark.anyio
async def test_the_daily_limit_defers_to_the_later_of_midnight_and_the_window(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5, daily_limit = 1,"
        " window_start = '01:00', window_end = '05:00', window_enabled = true",
    )
    # Job 10 runs, so the cap holds. The window opens after midnight.
    late = datetime(2026, 10, 21, 23, 30, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, late)
    assert enqueued == []
    assert jobs[12].deferred_until == datetime(2026, 10, 22, 1, 0, tzinfo=TIMEZONE)

    # Inside a window that crosses midnight, the cap waits for midnight.
    _sql(settings, "UPDATE download_settings SET window_start = '23:00'")
    enqueued, jobs = await _dispatch(settings, late)
    assert enqueued == []
    assert jobs[12].deferred_until == datetime(2026, 10, 22, 0, 0, tzinfo=TIMEZONE)


@pytest.mark.anyio
async def test_a_disabled_window_keeps_its_times_and_is_ignored(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5,"
        " window_start = '23:00', window_end = '05:00', window_enabled = false",
    )
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, noon)
    assert enqueued == [12, 13, 14]
    assert all(j.deferred_until is None for j in jobs.values())


@pytest.mark.anyio
async def test_a_worker_ignores_a_disabled_window(settings):
    _job_rows(settings, _closed_window_sql(), "UPDATE download_settings SET window_enabled = false")
    job, runner, _ = await _run(settings)
    assert runner.params is not None and job.attempts == 1


def test_api_daily_limit_window_switch_and_todays_count(settings):
    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
        " (2, 'archive', 'succeeded', 'busy1', 1, now()),"
        " (3, 'archive', 'running', 'busy1', 1, NULL),"
        f" (4, 'archive', 'succeeded', 'busy1', 1, {_KST_TODAY} - interval '1 second')",
    )
    body = {
        "max_concurrent": 1,
        "rate_limit_bytes": None,
        "window_start": "23:00",
        "window_end": "05:00",
        "window_enabled": False,
        "daily_limit": 3,
        "cookie_profile": None,
    }
    with _api(settings) as client:
        got = client.get("/api/download-settings").json()
        assert (got["daily_limit"], got["window_enabled"], got["downloaded_today"]) == (
            None,
            False,
            2,
        )
        put = client.put("/api/download-settings", json=body)
        assert put.status_code == 200
        got = client.get("/api/download-settings").json()
        assert {k: got[k] for k in body} == body
        assert got["downloaded_today"] == 2
        on = body | {"window_enabled": True, "daily_limit": None}
        assert client.put("/api/download-settings", json=on).status_code == 200
        got = client.get("/api/download-settings").json()
        assert (got["window_enabled"], got["daily_limit"]) == (True, None)
        for bad in (
            body | {"daily_limit": 0},
            body | {"window_enabled": True, "window_start": None, "window_end": None},
        ):
            assert client.put("/api/download-settings", json=bad).status_code == 422, bad


def test_migration_0013_enables_a_window_that_has_both_times(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0012")
    _sql(settings, "UPDATE download_settings SET window_start = '23:00', window_end = '05:00'")
    command.upgrade(cfg, "head")
    assert _sql_rows(
        settings, "SELECT window_enabled, window_start, daily_limit FROM download_settings"
    ) == [(True, dt_time(23, 0), None)]

    command.downgrade(cfg, "0012")
    _sql(settings, "UPDATE download_settings SET window_start = NULL, window_end = NULL")
    command.upgrade(cfg, "head")
    assert _sql_rows(settings, "SELECT window_enabled FROM download_settings") == [(False,)]
