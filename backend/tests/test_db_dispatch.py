from datetime import datetime

import pytest
from conftest import (
    ADMIN_URL,
    _api,
    _closed_window_sql,
    _dispatch,
    _job_rows,
    _run,
    _sql,
    _waiting_rows,
)

from savetube.db.models import (
    JobState,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


@pytest.mark.anyio
async def test_an_archive_outside_the_window_goes_back_to_waiting(settings):
    _job_rows(settings, _closed_window_sql())
    job, runner, queue_rows = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.attempts == 0
    # The dispatcher takes it again. Nothing new is queued.
    assert job.queue_job_id is None
    assert queue_rows == 0


@pytest.mark.anyio
async def test_an_archive_without_a_free_slot_goes_back_to_waiting(settings):
    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1)",
    )
    job, runner, queue_rows = await _run(settings)
    assert runner.params is None and job.state is JobState.QUEUED
    assert job.queue_job_id is None
    assert queue_rows == 0


@pytest.mark.anyio
async def test_an_admitted_archive_runs_with_the_rate_limit(settings):
    _job_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 2, rate_limit_bytes = 1000000",
        # A running recording takes no archive slot.
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1), (3, 'record', 'running', 'busy2', 1)",
    )
    job, runner, _ = await _run(settings)
    assert runner.params["ratelimit"] == 1000000
    assert job.attempts == 1 and job.started_at is not None
    assert job.state is JobState.FAILED  # the test runner fails every download


@pytest.mark.anyio
async def test_a_recording_ignores_every_download_limit(settings):
    _job_rows(
        settings,
        _closed_window_sql(),
        "UPDATE download_settings SET rate_limit_bytes = 1000",
        "UPDATE job SET kind = 'record' WHERE id = 1",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1)",
    )
    job, runner, _ = await _run(settings, live=True)
    assert runner.params is not None and "ratelimit" not in runner.params


@pytest.mark.anyio
async def test_dispatch_queues_the_oldest_waiting_archive_jobs_up_to_the_free_slots(settings):
    # One archive runs, so max_concurrent 2 leaves one slot. A recording takes none.
    _waiting_rows(settings, "UPDATE download_settings SET max_concurrent = 2")
    enqueued, jobs = await _dispatch(settings)
    assert enqueued == [12]
    assert jobs[12].queue_job_id is not None and jobs[12].state is JobState.QUEUED
    assert jobs[13].queue_job_id is None and jobs[15].queue_job_id is None

    # The dispatched job holds its slot until a worker takes it.
    enqueued, _ = await _dispatch(settings)
    assert enqueued == []

    # A queue job that ended without a worker taking the `job` row frees the slot.
    _sql(
        settings,
        "UPDATE procrastinate_jobs SET status = 'failed'",
    )
    enqueued, jobs = await _dispatch(settings)
    assert enqueued == [12]


@pytest.mark.anyio
async def test_a_waiting_archive_job_goes_before_an_older_replace_job(settings):
    # One archive runs, so max_concurrent 2 leaves one slot.
    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 2",
        "INSERT INTO job (id, kind, state, video_id, attempts, format_id) VALUES"
        " (9, 'replace', 'queued', 'live', 0, '251')",
    )
    enqueued, jobs = await _dispatch(settings)
    assert enqueued == [12]
    assert jobs[9].queue_job_id is None


@pytest.mark.anyio
async def test_a_replace_job_starts_when_no_archive_job_waits(settings):
    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 2",
        "DELETE FROM job WHERE kind = 'archive' AND state = 'queued'",
        "INSERT INTO job (id, kind, state, video_id, attempts, format_id) VALUES"
        " (19, 'replace', 'queued', 'live', 0, '251')",
    )
    enqueued, _ = await _dispatch(settings)
    assert enqueued == [19]


@pytest.mark.anyio
async def test_dispatch_with_max_concurrent_1_and_one_running_queues_nothing(settings):
    _waiting_rows(settings)
    enqueued, jobs = await _dispatch(settings)
    assert enqueued == []
    assert all(j.deferred_until is None for j in jobs.values())


@pytest.mark.anyio
async def test_dispatch_waits_for_the_window_and_shows_its_start(settings):
    from savetube.tz import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5,"
        " window_start = '23:00', window_end = '05:00', window_enabled = true",
    )
    # 12:00 in Seoul, eleven hours before the window opens.
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    opens = datetime(2026, 10, 21, 23, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, noon)
    assert enqueued == []
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [opens, opens, opens]
    assert jobs[15].deferred_until is None

    enqueued, jobs = await _dispatch(settings, opens)
    assert enqueued == [12, 13, 14]
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [None, None, None]


def test_api_download_settings_and_job_deferral(settings):
    _job_rows(settings, "UPDATE job SET deferred_until = '2026-10-21T14:00:00Z'")
    with _api(settings) as client:
        assert client.get("/api/download-settings").json() == {
            "max_concurrent": 1,
            "rate_limit_bytes": None,
            "window_start": None,
            "window_end": None,
            "window_enabled": False,
            "daily_limit": None,
            "cookie_profile": None,
            "timezone": "Asia/Seoul",
            "downloaded_today": 0,
            "blocked_until": None,
            "block_count": 0,
            "paused": False,
        }
        body = {
            "max_concurrent": 3,
            "rate_limit_bytes": 2000000,
            "window_start": "23:00",
            "window_end": "05:00",
            "window_enabled": True,
            "daily_limit": None,
            "cookie_profile": None,
        }
        put = client.put("/api/download-settings", json=body)
        assert put.status_code == 200
        unblocked = {"blocked_until": None, "block_count": 0, "paused": False}
        today = {"timezone": "Asia/Seoul", "downloaded_today": 0}
        assert put.json() == body | today | unblocked
        assert client.get("/api/download-settings").json() == put.json()
        for bad in (
            body | {"window_end": None},
            body | {"window_end": "23:00"},
            body | {"window_start": "25:00"},
            body | {"max_concurrent": 0},
            body | {"max_concurrent": 11},
            body | {"rate_limit_bytes": 0},
        ):
            assert client.put("/api/download-settings", json=bad).status_code == 422, bad
        open_all_day = body | {"window_start": None, "window_end": None, "window_enabled": False}
        assert client.put("/api/download-settings", json=open_all_day).status_code == 200

        [job] = client.get("/api/jobs").json()
        assert job["deferred_until"] == "2026-10-21T14:00:00Z"
