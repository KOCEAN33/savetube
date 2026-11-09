import logging
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    ADMIN_URL,
    MEMBERS_ONLY_ERROR,
    _api,
    _block_state,
    _events,
    _fields,
    _job_rows,
    _poll_outcome,
    _run_steps,
    _sql,
    _sql_rows,
    _Steps,
)

from savetube.db.models import JobState, VideoState

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

UNAVAILABLE = "DownloadError: ERROR: [youtube] v1: Video unavailable"
REMOVED = "DownloadError: ERROR: [youtube] v1: This video has been removed by the uploader"
PRIVATE = (
    "DownloadError: ERROR: [youtube] v1: Private video."
    " Sign in if you've been granted access to this video"
)
TERMINATED = (
    "DownloadError: ERROR: [youtube] v1: This video is no longer available because the"
    " YouTube account associated with this video has been terminated."
)
COPYRIGHT = (
    "DownloadError: ERROR: [youtube] v1: This video is no longer available due to a"
    " copyright claim by Some Label"
)


def _others(settings, *rows):
    """Other videos of channel UC1, each `(id, state, availability, completed hours ago)`."""

    def value(vid, state, avail, hours):
        availability = f"'{avail}'" if avail else "NULL"
        completed = "NULL" if hours is None else f"now() - interval '{hours} hours'"
        return f"('{vid}', 'UC1', 'videos', 't', '{state}', {availability}, {completed})"

    values = ", ".join(value(*row) for row in rows)
    _sql(
        settings,
        "INSERT INTO video (id, channel_id, type, title, state, availability, completed_at)"
        f" VALUES {values}",
    )


class _Probe:
    """Stands in for savetube.jobs.job.probe_video, recording each video id it gets."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __call__(self, video_id, cookiefile=None):
        self.calls.append(video_id)
        if self.error is not None:
            raise self.error
        return {"id": video_id, "availability": "public"}


def _fails_with(message):
    async def step(params, report):
        from savetube.jobs import DownloadFailed

        raise DownloadFailed(message)

    return _Steps(step)


async def _archive(settings, monkeypatch, caplog, message, probe):
    import savetube.jobs.job

    monkeypatch.setattr(savetube.jobs.job, "probe_video", probe)
    with caplog.at_level(logging.INFO, logger="savetube"):
        return await _run_steps(settings, _fails_with(message))


def _assert_gone_logged(caplog):
    [line] = _events(caplog, "video.gone")
    assert line.levelno == logging.INFO
    fields = _fields(line)
    assert (fields["job"], fields["video"]) == (1, "v1")


# A complete public video the bare-unavailable check could probe.
PROBEABLE = ("seen", "complete", "public", 5)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message",
    [REMOVED, PRIVATE, TERMINATED, COPYRIGHT],
    ids=["removed", "private", "terminated", "copyright"],
)
async def test_a_permanent_failure_fails_the_job_and_marks_the_video_gone(
    settings, monkeypatch, caplog, message
):
    _job_rows(settings)
    _others(settings, PROBEABLE)
    probe = _Probe()
    jobs, video = await _archive(settings, monkeypatch, caplog, message, probe)
    assert (jobs[1].state, jobs[1].log_tail) == (JobState.FAILED, message)
    assert video.state is VideoState.GONE
    assert len(_events(caplog, "job.failed")) == 1
    _assert_gone_logged(caplog)
    # The message itself says why, so no check runs.
    assert probe.calls == []
    assert _block_state(settings) == (0, None)


@pytest.mark.anyio
async def test_a_members_only_refusal_does_not_mark_the_video_gone(settings, monkeypatch, caplog):
    _job_rows(settings)
    _others(settings, PROBEABLE)
    jobs, video = await _archive(settings, monkeypatch, caplog, MEMBERS_ONLY_ERROR, _Probe())
    assert video.state is not VideoState.GONE
    assert _events(caplog, "video.gone") == []


@pytest.mark.anyio
async def test_a_lone_bare_unavailable_whose_check_plays_marks_the_video_gone(
    settings, monkeypatch, caplog
):
    _job_rows(settings)
    _others(
        settings,
        ("older", "complete", "public", 48),
        ("newest", "complete", "public", 2),
        # Newer, but not a complete public video.
        ("members", "complete", "subscriber_only", 1),
        ("broken", "failed", "public", None),
    )
    probe = _Probe()
    jobs, video = await _archive(settings, monkeypatch, caplog, UNAVAILABLE, probe)
    assert probe.calls == ["newest"]
    assert (jobs[1].state, jobs[1].log_tail) == (JobState.FAILED, UNAVAILABLE)
    assert video.state is VideoState.GONE
    assert len(_events(caplog, "job.failed")) == 1
    _assert_gone_logged(caplog)
    assert _block_state(settings) == (0, None)


@pytest.mark.anyio
async def test_a_lone_bare_unavailable_whose_check_fails_is_a_youtube_block(
    settings, monkeypatch, caplog
):
    from yt_dlp.utils import DownloadError

    _job_rows(settings, "SELECT setval('job_id_seq', 200)")
    _others(settings, PROBEABLE)
    probe = _Probe(DownloadError("ERROR: [youtube] seen: Video unavailable"))
    before = datetime.now(UTC)
    jobs, video = await _archive(settings, monkeypatch, caplog, UNAVAILABLE, probe)
    assert probe.calls == ["seen"]
    [history] = [j for j in jobs.values() if j.id != 1]
    assert (history.state, history.video_id) == ("interrupted", "v1")
    assert history.log_tail == f"blocked by YouTube: {UNAVAILABLE}"
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert video.state is VideoState.PENDING
    count, until = _block_state(settings)
    assert count == 1
    assert before + timedelta(minutes=30) <= until <= datetime.now(UTC) + timedelta(minutes=30)
    assert _events(caplog, "job.failed") == []
    assert _events(caplog, "video.gone") == []
    [line] = _events(caplog, "job.interrupted")
    assert _fields(line)["reason"] == "blocked by YouTube"
    [blocked] = _events(caplog, "dispatch.blocked")
    fields = _fields(blocked)
    assert (fields["count"], fields["job"], fields["video"]) == (1, 1, "v1")


@pytest.mark.anyio
async def test_a_lone_bare_unavailable_with_nothing_to_check_fails_as_before(
    settings, monkeypatch, caplog
):
    _job_rows(settings)
    _others(
        settings,
        ("members", "complete", "subscriber_only", 1),
        ("broken", "failed", "public", None),
    )
    probe = _Probe()
    jobs, video = await _archive(settings, monkeypatch, caplog, UNAVAILABLE, probe)
    assert probe.calls == []
    assert (jobs[1].state, jobs[1].log_tail) == (JobState.FAILED, UNAVAILABLE)
    assert video.state is VideoState.FAILED
    assert len(_events(caplog, "job.failed")) == 1
    assert _events(caplog, "video.gone") == []
    assert _block_state(settings) == (0, None)


def _streak(settings, *others):
    """The 2 other videos' archive jobs that failed on a bare "Video unavailable"
    within the last hour, with `others` as the other videos."""
    _job_rows(settings, "SELECT setval('job_id_seq', 200)")
    _others(settings, *others, ("other0", "failed", None, None), ("other1", "failed", None, None))
    _sql(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail, finished_at) VALUES"
        f" (100, 'archive', 'failed', 'other0', 1, '{UNAVAILABLE.replace('v1', 'other0')}',"
        "  now() - interval '20 minutes'),"
        f" (101, 'archive', 'failed', 'other1', 1, '{UNAVAILABLE.replace('v1', 'other1')}',"
        "  now() - interval '5 minutes')",
    )


@pytest.mark.anyio
async def test_a_streak_of_bare_unavailable_whose_check_plays_marks_the_video_gone(
    settings, monkeypatch, caplog
):
    # Deleted videos retried one after another end in a streak too.
    _streak(settings, PROBEABLE)
    probe = _Probe()
    jobs, video = await _archive(settings, monkeypatch, caplog, UNAVAILABLE, probe)
    assert probe.calls == ["seen"]
    assert (jobs[1].state, jobs[1].log_tail) == (JobState.FAILED, UNAVAILABLE)
    assert video.state is VideoState.GONE
    _assert_gone_logged(caplog)
    assert _events(caplog, "dispatch.blocked") == []
    assert _block_state(settings) == (0, None)


@pytest.mark.anyio
async def test_a_streak_of_bare_unavailable_with_nothing_to_check_is_a_youtube_block(
    settings, monkeypatch, caplog
):
    _streak(settings)
    probe = _Probe()
    jobs, video = await _archive(settings, monkeypatch, caplog, UNAVAILABLE, probe)
    assert probe.calls == []
    [history] = [j for j in jobs.values() if j.id not in (1, 100, 101)]
    assert history.state == "interrupted"
    assert (jobs[1].state, jobs[1].attempts) == (JobState.QUEUED, 0)
    assert video.state is VideoState.PENDING
    assert _block_state(settings)[0] == 1
    assert len(_events(caplog, "dispatch.blocked")) == 1
    assert _events(caplog, "video.gone") == []


@pytest.mark.anyio
async def test_the_poll_queues_nothing_for_a_gone_video_and_counts_it_as_given_up(settings, caplog):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Chan', 'Chan [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'videos', 't', 'gone')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@chan/videos', 'tab', 'UC1', true, 360)",
    )
    listing = {
        "channel_id": "UC1",
        "channel": "Chan",
        "entries": [{"id": "v1", "title": "t"}, {"id": "v2", "title": "t"}],
    }
    _, queued, _ = await _poll_outcome(settings, 1, listing, caplog)
    assert queued == ["v2"]
    [done] = _events(caplog, "poll.done")
    assert _fields(done)["gave_up"] == 1
    [(state,)] = _sql_rows(settings, "SELECT state FROM video WHERE id = 'v1'")
    assert state == "gone"


def _gone_and_failed(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Chan', 'Chan [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'videos', 't', 'gone'), ('v2', 'UC1', 'videos', 't', 'failed')",
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail, finished_at) VALUES"
        f" (1, 'archive', 'failed', 'v1', 1, '{REMOVED}', now() - interval '1 hour'),"
        f" (2, 'archive', 'failed', 'v2', 1, 'ERROR: x', now() - interval '2 hours')",
        "SELECT setval('job_id_seq', 2)",
    )


def test_api_jobs_carry_the_video_state_and_a_gone_video_is_not_retryable(settings):
    _gone_and_failed(settings)
    with _api(settings) as client:
        items = client.get("/api/jobs", params={"state": ["failed"]}).json()
        assert {j["id"]: (j["video_state"], j["retryable"]) for j in items} == {
            1: ("gone", False),
            2: ("failed", True),
        }
        assert client.post("/api/jobs/1/retry").status_code == 409
    assert len(_sql_rows(settings, "SELECT id FROM job")) == 2
    [(state,)] = _sql_rows(settings, "SELECT state FROM video WHERE id = 'v1'")
    assert state == "gone"


def test_api_unresolved_omits_the_failed_job_of_a_gone_video(settings):
    _gone_and_failed(settings)
    with _api(settings) as client:
        every = client.get("/api/jobs", params={"state": ["failed"]}).json()
        unresolved = client.get(
            "/api/jobs", params={"state": ["failed"], "unresolved": True}
        ).json()
    assert sorted(j["id"] for j in every) == [1, 2]
    assert [j["id"] for j in unresolved] == [2]
