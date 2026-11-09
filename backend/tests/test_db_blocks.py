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
    _profiles,
    _queue_row,
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
    from savetube.tz import TIMEZONE

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


UNAVAILABLE = "DownloadError: ERROR: [youtube] v1: Video unavailable"
REMOVED = (
    "DownloadError: ERROR: [youtube] v1: Video unavailable."
    " This video has been removed by the uploader"
)


def _ended_jobs(settings, *rows):
    """Archive jobs of other videos, each `(state, log_tail, minutes ago)`."""
    values = ", ".join(
        f"({100 + i}, 'archive', '{state}', 'other{i}', 1, '{tail}',"
        f" now() - interval '{minutes} minutes')"
        for i, (state, tail, minutes) in enumerate(rows)
    )
    videos = ", ".join(f"('other{i}', 'UC1', 'videos', 't', 'failed')" for i in range(len(rows)))
    _job_rows(
        settings,
        "SELECT setval('job_id_seq', 200)",
        f"INSERT INTO video (id, channel_id, type, title, state) VALUES {videos}",
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail, finished_at)"
        f" VALUES {values}",
    )


async def _unavailable_run(settings, message=UNAVAILABLE):
    async def unavailable(params, report):
        from savetube.jobs import DownloadFailed

        raise DownloadFailed(message)

    return await _run_steps(settings, _Steps(unavailable))


@pytest.mark.anyio
async def test_a_third_bare_unavailable_in_a_row_is_a_youtube_block(settings, caplog):
    import logging

    from savetube.web.api import job_reason

    _ended_jobs(
        settings,
        ("failed", UNAVAILABLE.replace("v1", "other0"), 20),
        ("failed", UNAVAILABLE.replace("v1", "other1") + ".", 5),
    )
    before = datetime.now(UTC)
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _unavailable_run(settings)
    [history] = [j for j in jobs.values() if j.id not in (1, 100, 101)]
    assert history.state == "interrupted"
    assert history.log_tail == f"blocked by YouTube: {UNAVAILABLE}"
    assert job_reason(history.log_tail) == "blocked by YouTube"
    assert (jobs[1].state, jobs[1].attempts) == (JobState.QUEUED, 0)
    assert video.state is VideoState.PENDING
    count, until = _block_state(settings)
    assert count == 1
    assert before + timedelta(minutes=30) <= until <= datetime.now(UTC) + timedelta(minutes=30)
    assert _events(caplog, "job.failed") == []
    assert len(_events(caplog, "dispatch.blocked")) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("ended", "message"),
    [
        # A success between the failures ends the streak.
        (
            [
                ("failed", UNAVAILABLE, 20),
                ("succeeded", None, 10),
                ("failed", UNAVAILABLE, 5),
            ],
            UNAVAILABLE,
        ),
        # The two failures before this one are more than an hour old.
        ([("failed", UNAVAILABLE, 70), ("failed", UNAVAILABLE, 61)], UNAVAILABLE),
        # A sub-reason is a video that is gone, not a refused client.
        ([("failed", REMOVED, 20), ("failed", UNAVAILABLE, 5)], UNAVAILABLE),
        ([("failed", UNAVAILABLE, 20), ("failed", UNAVAILABLE, 5)], REMOVED),
    ],
    ids=["success_between", "older_than_window", "earlier_sub_reason", "own_sub_reason"],
)
async def test_a_bare_unavailable_outside_a_streak_fails_the_job(settings, ended, message):
    _ended_jobs(
        settings,
        *[
            (state, (tail or "").replace("v1", f"other{i}"), m)
            for i, (state, tail, m) in enumerate(ended)
        ],
    )
    jobs, video = await _unavailable_run(settings, message)
    assert (jobs[1].state, jobs[1].log_tail) == (JobState.FAILED, message)
    # A removed video is gone, so no poll queues it again.
    assert video.state is (VideoState.GONE if message == REMOVED else VideoState.FAILED)
    assert _block_state(settings) == (0, None)


@pytest.mark.anyio
async def test_dispatch_sends_nothing_while_paused_and_leaves_no_deferral(settings):
    from savetube.tz import TIMEZONE

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
        limits = {"max_concurrent": 2, "window_enabled": False, "cookie_profile": None}
        assert client.put("/api/download-settings", json=limits).json()["paused"] is True
        assert client.put("/api/download-settings/paused", json={}).status_code == 422
        put = client.put("/api/download-settings/paused", json={"paused": False})
        assert put.json()["paused"] is False
    [paused] = _events(caplog, "dispatch.paused")
    [resumed] = _events(caplog, "dispatch.resumed")
    assert paused.levelno == resumed.levelno == logging.INFO


FORBIDDEN = "DownloadError: ERROR: unable to download video data: HTTP Error 403: Forbidden"


def _member_job_rows(settings, *extra):
    """Job 1 archives the members-only v1, whose channel's member profile is `main`."""
    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa'",
        "UPDATE video SET type = 'members' WHERE id = 'v1'",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        " ('UC1', 'main', true, now(), 'v1')",
        "INSERT INTO cookie_profile (name, seen_at) VALUES ('main', now())",
        "SELECT setval('job_id_seq', 200)",
        *extra,
    )
    return _profiles(settings, "main")


def _profile_state(settings, name):
    [row] = _sql_rows(
        settings, f"SELECT block_count, blocked_until FROM cookie_profile WHERE name = '{name}'"
    )
    return row.block_count, row.blocked_until


def _fails_with(message):
    async def step(params, report):
        from savetube.jobs import DownloadFailed

        raise DownloadFailed(message)

    # A 403 runs again once with the fallback clients.
    return _Steps(step, step)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("before_sql", "count", "backoff"),
    [
        ("block_count = 0, blocked_until = NULL", 1, timedelta(minutes=30)),
        # The pause after the first refusal is over: the next one doubles it.
        ("block_count = 1, blocked_until = now() - interval '1 minute'", 2, timedelta(hours=1)),
        ("block_count = 9, blocked_until = now() - interval '1 minute'", 10, timedelta(hours=12)),
    ],
    ids=["first", "second", "capped"],
)
async def test_a_403_with_a_member_profile_pauses_that_profile_only(
    settings, caplog, before_sql, count, backoff
):
    import logging

    settings = _member_job_rows(
        settings, f"UPDATE cookie_profile SET {before_sql} WHERE name = 'main'"
    )
    before = datetime.now(UTC)
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _fails_with(FORBIDDEN))
    [history] = [j for j in jobs.values() if j.id != 1]
    assert history.state == "interrupted"
    assert history.log_tail == f"refused for this cookie profile: {FORBIDDEN}"
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert video.state is VideoState.PENDING
    got_count, until = _profile_state(settings, "main")
    assert got_count == count
    assert before + backoff <= until <= datetime.now(UTC) + backoff
    assert _block_state(settings) == (0, None)
    assert _events(caplog, "job.failed") == []
    assert _events(caplog, "dispatch.blocked") == []
    [line] = _events(caplog, "dispatch.profile_blocked")
    assert line.levelno == logging.WARNING
    fields = _fields(line)
    assert (fields["profile"], fields["count"], fields["job"], fields["video"]) == (
        "main",
        count,
        1,
        "v1",
    )


@pytest.mark.anyio
async def test_a_403_while_the_profile_is_paused_does_not_lengthen_its_pause(settings, caplog):
    import logging

    settings = _member_job_rows(settings)

    async def refused_after_another(params, report):
        from savetube.jobs import DownloadFailed

        # Another job of this profile was refused while this one downloaded.
        _sql(
            settings,
            "UPDATE cookie_profile SET block_count = 1,"
            " blocked_until = now() + interval '10 minutes' WHERE name = 'main'",
        )
        raise DownloadFailed(FORBIDDEN)

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _Steps(refused_after_another, refused_after_another))
    assert (jobs[1].state, jobs[1].attempts) == (JobState.QUEUED, 0)
    count, until = _profile_state(settings, "main")
    assert count == 1 and until < datetime.now(UTC) + timedelta(minutes=11)
    assert _events(caplog, "dispatch.profile_blocked") == []


@pytest.mark.anyio
async def test_a_403_of_a_cookie_free_run_is_a_youtube_block(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    before = datetime.now(UTC)
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _fails_with(FORBIDDEN))
    [history] = [j for j in jobs.values() if j.id != 1]
    assert history.state == "interrupted"
    assert history.log_tail == f"blocked by YouTube: {FORBIDDEN}"
    assert (jobs[1].state, jobs[1].attempts) == (JobState.QUEUED, 0)
    count, until = _block_state(settings)
    assert count == 1
    assert before + timedelta(minutes=30) <= until <= datetime.now(UTC) + timedelta(minutes=30)
    assert _events(caplog, "dispatch.profile_blocked") == []


@pytest.mark.anyio
async def test_a_youtube_block_wins_over_a_403_with_a_profile(settings):
    settings = _member_job_rows(settings)
    message = f"{BOT_CHECK} {FORBIDDEN}"
    jobs, _ = await _run_steps(settings, _fails_with(message))
    [history] = [j for j in jobs.values() if j.id != 1]
    assert history.log_tail == f"blocked by YouTube: {message}"
    assert _block_state(settings)[0] == 1
    assert _profile_state(settings, "main") == (0, None)


@pytest.mark.anyio
async def test_a_success_with_the_profile_after_its_pause_resets_it(settings, caplog):
    import logging

    settings = _member_job_rows(
        settings,
        "UPDATE cookie_profile SET block_count = 2, blocked_until = now() - interval '1 minute'"
        " WHERE name = 'main'",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _Steps(_finished()))
    assert jobs[1].state is JobState.SUCCEEDED
    assert _profile_state(settings, "main") == (0, None)
    [line] = _events(caplog, "dispatch.profile_unblocked")
    assert (_fields(line)["profile"], _fields(line)["count"]) == ("main", 2)


@pytest.mark.anyio
async def test_a_success_while_the_profile_pause_holds_resets_nothing(settings, caplog):
    import logging

    settings = _member_job_rows(
        settings, "UPDATE cookie_profile SET block_count = 1 WHERE name = 'main'"
    )
    finish = _finished()

    async def refused_meanwhile(params, report):
        # Another job of this profile was refused while this one downloaded.
        _sql(
            settings,
            "UPDATE cookie_profile SET block_count = 2,"
            " blocked_until = now() + interval '10 minutes' WHERE name = 'main'",
        )
        return await finish(params, report)

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _Steps(refused_meanwhile))
    assert jobs[1].state is JobState.SUCCEEDED
    count, until = _profile_state(settings, "main")
    assert count == 2 and until > datetime.now(UTC)
    assert _events(caplog, "dispatch.profile_unblocked") == []


@pytest.mark.anyio
async def test_a_worker_hands_back_a_job_whose_profile_is_paused(settings, caplog):
    import logging

    settings = _member_job_rows(
        settings,
        "UPDATE cookie_profile SET block_count = 1,"
        " blocked_until = now() + interval '1 hour' WHERE name = 'main'",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _fails_with("never downloaded"))
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert list(jobs) == [1]
    [line] = _events(caplog, "job.returned")
    assert _fields(line)["reason"] == "profile_blocked"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("general", "blocked", "enqueued", "deferred"),
    [
        # The member profile is paused: its members-only job waits, others go.
        ("burner", "main", [13, 14], [12]),
        (None, "main", [13, 14], [12]),
        # The general profile is paused: the members-only job still goes.
        ("burner", "burner", [12], [13, 14]),
    ],
    ids=["member_paused", "member_paused_cookie_free", "general_paused"],
)
async def test_dispatch_skips_the_jobs_of_a_paused_profile(
    settings, general, blocked, enqueued, deferred
):
    until = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
    general_sql = f"'{general}'" if general else "NULL"
    _waiting_rows(
        settings,
        "UPDATE job SET state = 'succeeded' WHERE id = 10",
        "UPDATE video SET type = 'members' WHERE id = 'a'",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        " ('UC1', 'main', true, now(), 'a')",
        "INSERT INTO cookie_profile (name, seen_at) VALUES ('main', now()), ('burner', now())",
        f"UPDATE download_settings SET max_concurrent = 5, cookie_profile = {general_sql}",
        f"UPDATE cookie_profile SET block_count = 1, blocked_until = '{until.isoformat()}'"
        f" WHERE name = '{blocked}'",
    )
    got, jobs = await _dispatch(settings, until - timedelta(minutes=1))
    assert got == enqueued
    assert {i: jobs[i].deferred_until for i in (12, 13, 14)} == {
        i: (until if i in deferred else None) for i in (12, 13, 14)
    }
    assert _block_state(settings) == (0, None)


def _refused(params, report):
    from savetube.jobs import DownloadFailed

    raise DownloadFailed(FORBIDDEN)


def _clients(params):
    return params.get("extractor_args", {}).get("youtube", {}).get("player_client")


@pytest.mark.anyio
async def test_a_403_runs_again_with_the_mweb_client_before_any_profile_block(settings, caplog):
    import logging

    settings = _member_job_rows(settings)
    runner = _Steps(_refused, _finished())
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, runner)
    assert jobs[1].state is JobState.SUCCEEDED
    assert list(jobs) == [1]
    assert len(runner.calls) == 2
    assert _clients(runner.calls[0]) != ["mweb"]
    assert _clients(runner.calls[1]) == ["mweb"]
    assert _profile_state(settings, "main") == (0, None)
    assert _events(caplog, "dispatch.profile_blocked") == []
    [line] = _events(caplog, "download.client_fallback")
    assert line.levelno == logging.WARNING


@pytest.mark.anyio
async def test_a_403_after_the_client_fallback_too_pauses_the_profile(settings, caplog):
    import logging

    settings = _member_job_rows(settings)
    runner = _Steps(_refused, _refused)
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, runner)
    assert len(runner.calls) == 2
    assert _clients(runner.calls[1]) == ["mweb"]
    assert (jobs[1].state, jobs[1].attempts) == (JobState.QUEUED, 0)
    assert _profile_state(settings, "main")[0] == 1
    assert len(_events(caplog, "dispatch.profile_blocked")) == 1


def _no_member_sql():
    """Video `a` is members-only, and `main`, the one present profile, is no member."""
    return (
        "UPDATE job SET state = 'succeeded' WHERE id = 10",
        "UPDATE video SET type = 'members' WHERE id = 'a'",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        " ('UC1', 'main', false, now(), 'a')",
        "INSERT INTO cookie_profile (name, seen_at) VALUES ('main', now())",
        "UPDATE download_settings SET max_concurrent = 5",
    )


@pytest.mark.anyio
async def test_dispatch_skips_a_members_only_job_with_no_member_and_sets_no_deferral(settings):
    _waiting_rows(settings, *_no_member_sql())
    enqueued, jobs = await _dispatch(settings, datetime(2026, 10, 21, 3, 0, tzinfo=UTC))
    assert enqueued == [13, 14]
    assert jobs[12].state is JobState.QUEUED and jobs[12].queue_job_id is None
    assert jobs[12].deferred_until is None


@pytest.mark.anyio
async def test_a_worker_hands_back_a_members_only_job_with_no_member(settings, caplog):
    import logging

    _job_rows(
        settings,
        "UPDATE video SET type = 'members' WHERE id = 'v1'",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        " ('UC1', 'main', false, now(), 'v1')",
        "INSERT INTO cookie_profile (name, seen_at) VALUES ('main', now())",
    )
    settings = _profiles(settings, "main")
    runner = _Steps()
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, runner)
    assert runner.calls == []
    assert list(jobs) == [1]
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert jobs[1].deferred_until is None
    [line] = _events(caplog, "job.returned")
    assert _fields(line)["reason"] == "no_member"


async def _dispatch_logged(settings, now, caplog):
    """One real dispatch, reported through the scheduler's log."""
    import logging

    from savetube.jobs import dispatch
    from savetube.scheduler import DispatchLog

    async def enqueue(job_id):
        return _queue_row(settings)

    engine = make_engine(settings.sqlalchemy_url)
    async with make_sessionmaker(engine)() as session:
        d = await dispatch(session, now, enqueue)
    await engine.dispose()
    with caplog.at_level(logging.INFO, logger="savetube"):
        DispatchLog(clock=lambda: 0.0)(d)
    return d


@pytest.mark.anyio
async def test_dispatch_waiting_names_the_paused_profile_holding_every_job(settings, caplog):
    from savetube.tz import TIMEZONE

    until = datetime(2026, 10, 21, 18, 30, tzinfo=TIMEZONE)
    _waiting_rows(
        settings,
        "UPDATE job SET state = 'succeeded' WHERE id = 10",
        "UPDATE video SET type = 'members' WHERE id IN ('a', 'b', 'c')",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        " ('UC1', 'main', true, now(), 'a')",
        "INSERT INTO cookie_profile (name, seen_at) VALUES ('main', now())",
        "UPDATE download_settings SET max_concurrent = 5",
        f"UPDATE cookie_profile SET block_count = 1, blocked_until = '{until.isoformat()}'"
        " WHERE name = 'main'",
    )
    d = await _dispatch_logged(settings, until - timedelta(hours=1), caplog)
    assert d.started == []
    [line] = _events(caplog, "dispatch.waiting")
    fields = _fields(line)
    assert fields["reason"] == "profile_blocked"
    assert fields["profiles"] == "main@18:30(3)"


@pytest.mark.anyio
async def test_dispatch_waiting_counts_members_only_jobs_with_no_member(settings, caplog):
    _waiting_rows(
        settings,
        *_no_member_sql(),
        "UPDATE video SET type = 'members' WHERE id IN ('b', 'c')",
    )
    d = await _dispatch_logged(settings, datetime(2026, 10, 21, 3, 0, tzinfo=UTC), caplog)
    assert d.started == []
    [line] = _events(caplog, "dispatch.waiting")
    fields = _fields(line)
    assert (fields["reason"], fields["no_member"]) == ("no_member", 3)


@pytest.mark.anyio
async def test_a_degraded_format_list_with_a_member_profile_pauses_that_profile(settings, caplog):
    """Throttling served a members-only video with only 360p format 18: the
    profile pauses as on a 403, and the attempt does not count."""
    import logging

    from savetube.ytdl import DEGRADED_MARKER

    degraded = f"DegradedFormats: YouTube offered {DEGRADED_MARKER}: 18"
    settings = _member_job_rows(settings)
    before = datetime.now(UTC)
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _fails_with(degraded))
    [history] = [j for j in jobs.values() if j.id != 1]
    assert history.state == "interrupted"
    assert degraded in history.log_tail
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert video.state is VideoState.PENDING
    count, until = _profile_state(settings, "main")
    assert count == 1
    backoff = timedelta(minutes=30)
    assert before + backoff <= until <= datetime.now(UTC) + backoff
    assert _block_state(settings) == (0, None)
    assert _events(caplog, "job.failed") == []
    assert _events(caplog, "dispatch.blocked") == []
    [line] = _events(caplog, "dispatch.profile_blocked")
    assert line.levelno == logging.WARNING
    fields = _fields(line)
    assert (fields["profile"], fields["count"], fields["job"], fields["video"]) == (
        "main",
        1,
        1,
        "v1",
    )
