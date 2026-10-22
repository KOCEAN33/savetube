import asyncio

import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    _api,
    _events,
    _FakeTools,
    _fields,
    _finished,
    _home,
    _job_rows,
    _part,
    _run,
    _run_steps,
    _sql,
    _sql_rows,
    _Steps,
)

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Job,
    JobState,
    Video,
    VideoState,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


class _ProgressRunner:
    """Reports one progress update, then fails like a stopped download."""

    PROGRESS = {
        "format": "140",
        "pct": 25.0,
        "done": 1048576,
        "total": 4194304,
        "speed": 524288.0,
        "eta": 6,
    }

    async def __call__(self, url, params, report):
        from savetube.jobs import DownloadFailed

        await report(self.PROGRESS)
        raise DownloadFailed("stopped by the test")


@pytest.mark.anyio
async def test_a_download_stores_its_progress_on_the_job_row_for_the_api(settings):
    from savetube.jobs import DownloadFailed, run

    _job_rows(settings)
    engine = make_engine(settings.sqlalchemy_url)
    with pytest.raises(DownloadFailed):
        await run(
            1,
            settings,
            live=False,
            runner=_ProgressRunner(),
            sessionmaker=make_sessionmaker(engine),
        )
    await engine.dispose()
    with _api(settings) as client:
        [job] = client.get("/api/jobs", params={"channel_id": "UC1"}).json()
    assert job["progress"] == _ProgressRunner.PROGRESS


@pytest.mark.anyio
async def test_a_failed_archive_logs_its_attempt_and_the_third_is_an_error(settings, caplog):
    import logging

    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (9, 'archive', 'failed', 'v1', 1)",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _run(settings)
    events = {r.getMessage(): r for r in caplog.records if r.name == "savetube.jobs"}
    start = _fields(events["job.start"])
    assert (start["job"], start["video"], start["channel"], start["title"]) == (1, "v1", "A", "t")
    assert start["attempt"] == "2/3" and start["cookies"] == "none"
    failed = events["job.failed"]
    assert failed.levelno == logging.WARNING
    assert _fields(failed)["attempt"] == "2/3"
    assert _fields(failed)["reason"] == "DownloadFailed: stopped by the test"
    assert failed.exc_info is None

    caplog.clear()
    # The poll's retry is a new job row, after two failed ones.
    _sql(
        settings,
        "UPDATE job SET id = 10 WHERE id = 1",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (1, 'archive', 'queued', 'v1', 0)",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _run(settings)
    [failed] = [r for r in caplog.records if r.getMessage() == "job.failed"]
    assert failed.levelno == logging.ERROR and _fields(failed)["attempt"] == "3/3"


def _failing(message):
    async def step(params, report):
        from savetube.jobs import DownloadFailed

        raise DownloadFailed(message)

    return step


@pytest.mark.anyio
async def test_the_job_row_names_the_step_after_the_last_byte(settings):
    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    seen = []

    class Tools(_FakeTools):
        def probe(self, path):
            # The checks and the audio copy both start with a probe.
            [(progress,)] = _sql_rows(settings, "SELECT progress FROM job WHERE id = 1")
            seen.append(progress)
            return super().probe(path)

    jobs, _ = await _run_steps(settings, _Steps(_finished()), tools=Tools())

    assert jobs[1].state is JobState.SUCCEEDED
    assert seen == [{"step": "verifying"}, {"step": "audio"}]


@pytest.mark.anyio
async def test_a_worker_shutdown_interrupts_the_download_and_keeps_its_parts(settings, caplog):
    import logging

    from savetube.jobs import run

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    started = asyncio.Event()

    async def downloading(params, report):
        _part(params)
        await report({"event": "format", "format_id": "251"})
        started.set()
        await asyncio.Event().wait()

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    with caplog.at_level(logging.INFO, logger="savetube"):
        task = asyncio.create_task(
            run(1, settings, live=False, runner=_Steps(downloading), sessionmaker=sessionmaker)
        )
        await asyncio.wait_for(started.wait(), 10)
        # procrastinate cancels the task when the worker shuts down.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
        video = await session.get(Video, "v1")
    await engine.dispose()

    [history] = [j for j in jobs.values() if j.id != 1]
    assert (history.state, history.video_id) == ("interrupted", "v1")
    assert history.log_tail == "worker shut down during the download"
    assert history.attempts == 1 and history.finished_at is not None
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert jobs[1].started_at is None and jobs[1].progress is None
    assert video.state is VideoState.PENDING and video.resume_format == "251"
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048
    [line] = _events(caplog, "job.interrupted")
    assert line.levelno == logging.INFO
    assert (_fields(line)["job"], _fields(line)["video"]) == (1, "v1")


@pytest.mark.anyio
@pytest.mark.parametrize("reported", [True, False])
async def test_an_interruption_clears_progress_to_sql_null(settings, reported):
    from savetube.jobs import run

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    started = asyncio.Event()

    async def downloading(params, report):
        if reported:
            await report({"percent": 50})
        started.set()
        await asyncio.Event().wait()

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    task = asyncio.create_task(
        run(1, settings, live=False, runner=_Steps(downloading), sessionmaker=sessionmaker)
    )
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await engine.dispose()
    # A JSON null is IS NOT NULL in SQL, so the hourly cleanup would count it.
    rows = _sql_rows(settings, "SELECT id, progress FROM job WHERE progress IS NOT NULL")
    assert rows == ([(201, {"percent": 50})] if reported else [])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("chat", "langs"),
    [
        # yt-dlp writes a subtitle to the temp path and renames a finished chat
        # from its `.part`, so a file under its final name is complete.
        (".tmp/v1.live_chat.json", ["all", "-live_chat"]),
        ("v1.live_chat.json", ["all", "-live_chat"]),
        (".tmp/v1.live_chat.json.part", ["all"]),
        (None, ["all"]),
    ],
)
async def test_a_resumed_run_skips_a_finished_chat_replay(settings, chat, langs):
    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    workdir = settings.incoming_root / "v1"
    if chat is not None:
        (workdir / chat).parent.mkdir(parents=True, exist_ok=True)
        (workdir / chat).write_text("{}")
    runner = _Steps(_finished())
    await _run_steps(settings, runner)
    [params] = runner.calls
    assert params["subtitleslangs"] == langs


@pytest.mark.anyio
async def test_a_retry_resumes_the_parts_with_the_pinned_format_and_counts_no_attempt(
    settings, caplog
):
    import logging

    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa'",
        "UPDATE video SET resume_format = '251' WHERE id = 'v1'",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (8, 'archive', 'interrupted', 'v1', 1), (9, 'archive', 'interrupted', 'v1', 1)",
    )
    _part({"paths": {"home": str(settings.incoming_root / "v1")}})
    tools = _FakeTools()
    runner = _Steps(_finished())
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner, tools)
    [params] = runner.calls
    assert params["format"] == "251" and params["continuedl"] is True
    assert _home(params) == settings.incoming_root / "v1"
    # Interrupted runs are not failed attempts.
    assert _fields(_events(caplog, "job.start")[0])["attempt"] == "1/3"
    [resume] = _events(caplog, "download.resume")
    assert resume.levelno == logging.INFO
    assert _fields(resume)["present"] == "2KiB" and _fields(resume)["format"] == "251"
    assert jobs[1].state is JobState.SUCCEEDED
    # A resumed download gets the packet pass.
    assert tools.packet_runs == 1
    assert video.verified_checks == ["size", "probe", "packets"]
    assert video.resume_format is None
    assert not (settings.incoming_root / "v1").exists()


@pytest.mark.anyio
async def test_a_pinned_format_no_longer_offered_starts_over_and_a_real_failure_clears_the_parts(
    settings, caplog
):
    import logging

    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa'",
        "UPDATE video SET resume_format = '251' WHERE id = 'v1'",
    )
    old = {"paths": {"home": str(settings.incoming_root / "v1")}}
    _part(old)
    seen = []

    async def gone_at_retry(params, report):
        from savetube.jobs import DownloadFailed

        seen.append(sorted(p.name for p in _home(params).rglob("*")))
        raise DownloadFailed("HTTP Error 403: Forbidden")

    runner = _Steps(
        _failing(
            "DownloadError: ERROR: [youtube] v1: Requested format is not available."
            " Use --list-formats for a list of available formats"
        ),
        gone_at_retry,
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner)
    assert [p["format"] for p in runner.calls] == ["251", "wa"]
    # The restart downloads into an empty directory.
    assert seen == [[]]
    [restart] = _events(caplog, "download.restart")
    assert restart.levelno == logging.WARNING and _fields(restart)["format"] == "251"
    assert jobs[1].state is JobState.FAILED and jobs[1].log_tail == "HTTP Error 403: Forbidden"
    assert video.resume_format is None
    assert not (settings.incoming_root / "v1").exists()


@pytest.mark.anyio
async def test_a_fresh_download_is_verified_without_the_packet_pass(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    tools = _FakeTools()
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(_finished()), tools)
    assert jobs[1].state is JobState.SUCCEEDED and video.state is VideoState.COMPLETE
    assert tools.packet_runs == 0
    assert video.verified_checks == ["size", "probe"] and video.verified_at is not None
    assert _events(caplog, "download.resume") == []
    [verified] = _events(caplog, "download.verified")
    assert verified.levelno == logging.INFO and _fields(verified)["checks"] == "size,probe"
    with _api(settings) as client:
        detail = client.get("/api/videos/v1").json()
    assert detail["verified_checks"] == ["size", "probe"] and detail["verified_at"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize("second_ok", [True, False])
async def test_an_integrity_failure_downloads_again_from_zero_once(settings, caplog, second_ok):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    seen = []

    def short_stream():
        inner = _finished(stream_size=4, expected=5)

        async def step(params, report):
            seen.append(sorted(p.name for p in _home(params).rglob("*")))
            return await inner(params, report)

        return step

    second = _finished() if second_ok else short_stream()
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(short_stream(), second))
    [first, *rest] = _events(caplog, "download.integrity_failed")
    assert first.levelno == logging.WARNING
    assert _fields(first)["check"] == "size"
    assert (_fields(first)["size"], _fields(first)["expected"]) == (4, 5)
    if second_ok:
        assert jobs[1].state is JobState.SUCCEEDED and rest == []
    else:
        # The second run started from an empty directory too.
        assert seen == [[], []]
        assert jobs[1].state is JobState.FAILED
        assert jobs[1].log_tail.startswith("integrity check failed: size")
        assert not (settings.incoming_root / "v1").exists()
    # One job row, one attempt, either way.
    assert list(jobs) == [1] and jobs[1].attempts == 1
