import logging

import pytest
from conftest import (
    ADMIN_URL,
    _api,
    _events,
    _fields,
    _poll_outcome,
    _sql,
    _sql_rows,
)
from fastapi.testclient import TestClient

from savetube.web.app import create_app
from savetube.web.auth import User, current_user

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

EVERY_STATE = {
    "state": ["queued", "running", "succeeded", "failed", "missed", "cancelled", "interrupted"]
}
JOB_COLUMNS = "SELECT id, kind, state, video_id, attempts, queue_job_id, log_tail FROM job"


def _three_failures(settings, *extra, video_state="failed"):
    """Video v1 whose three archive jobs failed, the last one just now."""
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Chan', 'Chan [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('v1', 'UC1', 'videos', 't', '{video_state}')",
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail, finished_at) VALUES"
        " (1, 'archive', 'failed', 'v1', 1, 'ERROR: x', now() - interval '3 hours'),"
        " (2, 'archive', 'failed', 'v1', 2, 'ERROR: x', now() - interval '2 hours'),"
        " (3, 'archive', 'failed', 'v1', 3, 'ERROR: x', now() - interval '1 hour')",
        "SELECT setval('job_id_seq', 3)",
        *extra,
    )


def _retryable(client):
    return {j["id"]: j["retryable"] for j in client.get("/api/jobs", params=EVERY_STATE).json()}


def _video_state(settings, video_id="v1"):
    [(state,)] = _sql_rows(settings, f"SELECT state FROM video WHERE id = '{video_id}'")
    return state


def test_only_the_newest_failed_archive_job_is_retryable_and_a_retry_queues_a_new_job(
    settings, caplog
):
    _three_failures(settings)
    before = _sql_rows(settings, f"{JOB_COLUMNS} ORDER BY id")
    with _api(settings) as client:
        assert _retryable(client) == {1: False, 2: False, 3: True}

        with caplog.at_level(logging.INFO, logger="savetube"):
            response = client.post("/api/jobs/3/retry")
        assert response.status_code == 200
        new_id = response.json()["job_id"]
        assert new_id not in (1, 2, 3)

        [new] = _sql_rows(settings, f"{JOB_COLUMNS} WHERE id > 3")
        assert (new.id, new.kind, new.state, new.video_id, new.queue_job_id) == (
            new_id,
            "archive",
            "queued",
            "v1",
            None,
        )
        # The failed rows stay as they were.
        assert _sql_rows(settings, f"{JOB_COLUMNS} WHERE id <= 3 ORDER BY id") == before
        assert _video_state(settings) == "pending"

        [retried] = _events(caplog, "job.retried")
        fields = _fields(retried)
        assert (fields["job"], fields["video"], fields["new_job"]) == (3, "v1", new_id)

        # A newer job now exists for v1, so job 3 is no longer retryable.
        assert client.post("/api/jobs/3/retry").status_code == 409
        retryable = _retryable(client)
        assert retryable[3] is False
        assert retryable[new_id] is False
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 4


def test_an_older_failed_job_answers_409(settings):
    _three_failures(settings)
    with _api(settings) as client:
        assert client.post("/api/jobs/2/retry").status_code == 409
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 3
    assert _video_state(settings) == "failed"


@pytest.mark.parametrize(
    "newer",
    [
        "(4, 'record', 'cancelled', 'v1', 0)",
        "(4, 'replace', 'succeeded', 'v1', 1)",
    ],
    ids=["record_cancelled", "replace_succeeded"],
)
# An interrupted row records a run of an earlier job, so it is no newer job
# (test_a_newer_interrupted_row_does_not_stop_the_failed_job_from_being_retryable).
def test_a_newer_job_of_any_kind_or_state_makes_the_failed_job_not_retryable(settings, newer):
    _three_failures(
        settings,
        f"INSERT INTO job (id, kind, state, video_id, attempts) VALUES {newer}",
        "SELECT setval('job_id_seq', 4)",
    )
    with _api(settings) as client:
        assert _retryable(client)[3] is False
        assert client.post("/api/jobs/3/retry").status_code == 409
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 4


@pytest.mark.parametrize("video_state", ["complete", "staged", "deleted"])
def test_a_failed_job_of_a_finished_or_deleted_video_is_not_retryable(settings, video_state):
    _three_failures(settings, video_state=video_state)
    with _api(settings) as client:
        assert _retryable(client)[3] is False
        assert client.post("/api/jobs/3/retry").status_code == 409
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 3
    assert _video_state(settings) == video_state


@pytest.mark.parametrize(
    ("kind", "state", "video_type"),
    [
        ("archive", "succeeded", "videos"),
        ("archive", "interrupted", "videos"),
        ("record", "failed", "lives"),
    ],
    ids=["succeeded", "interrupted", "record"],
)
def test_only_a_failed_archive_job_is_retryable(settings, kind, state, video_type):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Chan', 'Chan [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('v1', 'UC1', '{video_type}', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        f" (1, '{kind}', '{state}', 'v1', 1)",
        "SELECT setval('job_id_seq', 1)",
    )
    with _api(settings) as client:
        assert _retryable(client) == {1: False}
        assert client.post("/api/jobs/1/retry").status_code == 409
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 1


def test_retry_of_an_unknown_job_answers_404(settings):
    _three_failures(settings)
    with _api(settings) as client:
        assert client.post("/api/jobs/999999/retry").status_code == 404
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 3


def test_retry_refuses_a_write_without_the_csrf_header(settings):
    _three_failures(settings)
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    with TestClient(app) as client:
        assert client.post("/api/jobs/3/retry").status_code == 403
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 3
    assert _video_state(settings) == "failed"


@pytest.mark.anyio
async def test_a_retry_queues_a_job_for_a_video_the_poll_gave_up_on(settings, caplog):
    _three_failures(
        settings,
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@chan/videos', 'tab', 'UC1', true, 360)",
    )
    listing = {"channel_id": "UC1", "channel": "Chan", "entries": [{"id": "v1", "title": "t"}]}

    _, queued, _ = await _poll_outcome(settings, 1, listing, caplog)
    [done] = _events(caplog, "poll.done")
    assert _fields(done)["gave_up"] == 1
    assert queued == []

    with _api(settings) as client:
        response = client.post("/api/jobs/3/retry")
    assert response.status_code == 200
    new_id = response.json()["job_id"]

    await _poll_outcome(settings, 1, listing, caplog)
    # The next poll leaves the retry's job queued.
    [new] = _sql_rows(settings, f"{JOB_COLUMNS} WHERE id = {new_id}")
    assert (new.kind, new.state, new.video_id) == ("archive", "queued", "v1")


def _interrupted_after_failures(settings, *extra):
    """Video v1 as in production: three failed archive jobs, the last at 11:22, and the
    history row an earlier block wrote at 03:22, which has the highest id."""
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Chan', 'Chan [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'videos', 't', 'failed')",
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail, finished_at) VALUES"
        " (2721, 'archive', 'failed', 'v1', 1, 'ERROR: x', now() - interval '30 hours'),"
        " (4129, 'archive', 'failed', 'v1', 2, 'ERROR: x', now() - interval '20 hours'),"
        " (4340, 'archive', 'failed', 'v1', 3, 'ERROR: x', now() - interval '1 hour'),"
        " (4472, 'archive', 'interrupted', 'v1', 0,"
        "  'blocked by YouTube: DownloadError: ERROR: [youtube] v1: Video unavailable',"
        "  now() - interval '9 hours')",
        "SELECT setval('job_id_seq', 4472)",
        *extra,
    )


def test_a_newer_interrupted_row_does_not_stop_the_failed_job_from_being_retryable(
    settings,
):
    _interrupted_after_failures(settings)
    with _api(settings) as client:
        assert _retryable(client) == {2721: False, 4129: False, 4340: True, 4472: False}
        response = client.post("/api/jobs/4340/retry")
        assert response.status_code == 200
        new_id = response.json()["job_id"]
        [new] = _sql_rows(settings, f"{JOB_COLUMNS} WHERE id > 4472")
        assert (new.id, new.kind, new.state, new.video_id) == (new_id, "archive", "queued", "v1")
        assert _video_state(settings) == "pending"
        # The retry's queued job is a newer job that is no history row.
        assert client.post("/api/jobs/4340/retry").status_code == 409
        assert _retryable(client)[4340] is False
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 5


def test_api_unresolved_drops_an_interrupted_row_a_later_failure_followed(settings):
    _interrupted_after_failures(
        settings,
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v2', 'UC1', 'videos', 't', 'pending'), ('v3', 'UC1', 'videos', 't', 'failed')",
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail, finished_at) VALUES"
        # v2: the interrupted row and its job, queued again, with nothing finished after it.
        " (5000, 'archive', 'queued', 'v2', 0, NULL, NULL),"
        " (5001, 'archive', 'interrupted', 'v2', 0, 'blocked by YouTube: x',"
        "  now() - interval '2 hours'),"
        # v3: the failure finished before the interrupted row, not after it.
        " (5002, 'archive', 'failed', 'v3', 1, 'ERROR: x', now() - interval '3 hours'),"
        " (5003, 'archive', 'interrupted', 'v3', 0, 'blocked by YouTube: x',"
        "  now() - interval '2 hours')",
        "SELECT setval('job_id_seq', 5003)",
    )
    params = {"state": ["failed", "interrupted"], "unresolved": True}
    with _api(settings) as client:
        ids = {j["id"] for j in client.get("/api/jobs", params=params).json()}
    assert 4472 not in ids
    assert 4340 in ids
    assert {5001, 5003} <= ids
