import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    _block_state,
    _events,
    _FakeTools,
    _fields,
    _finished,
    _home,
    _job_rows,
    _part,
    _run_steps,
    _Steps,
)

from savetube.db.models import (
    JobState,
    VideoState,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


READ_TIMEOUT = (
    "DownloadError: ERROR: \r[download] Got error: HTTPSConnectionPool("
    "host='rr7---sn-3u-bh2zl.googlevideo.com', port=443): Read timed out. (read timeout=20.0)"
)


def _network_step(message=READ_TIMEOUT, progress=True):
    async def step(params, report):
        from savetube.jobs import DownloadFailed

        if progress:
            _part(params)
        await report({"event": "format", "format_id": "251"})
        raise DownloadFailed(message)

    return step


SERVER_500 = (
    "DownloadError: ERROR: \r[download] Got error: HTTP Error 500: Internal Server Error."
    " Giving up after 10 retries"
)


EMPTY_FILE = "DownloadError: ERROR: The downloaded file is empty"


def _server_error(chosen, *, finished=(), added=0, suffix=".part", message=SERVER_500):
    """A run that picks `chosen`, finishes the streams in `finished`, writes
    `added` bytes of the next one into `v1.f<id>.mp4<suffix>`, and ends on
    `message`."""

    async def step(params, report):
        from savetube.jobs import DownloadFailed

        if chosen is not None:
            await report({"event": "format", "format_id": chosen})
        for format_id in finished:
            await report({"event": "stream", "format_id": format_id, "size": 5, "expected": 5})
        if added:
            failing = next(f for f in chosen.split("+") if f not in finished)
            _part(params, f"v1.f{failing}.mp4{suffix}", added)
        raise DownloadFailed(message)

    return step


def _picks_then_finishes(chosen, seen=None):
    inner = _finished()

    async def step(params, report):
        if seen is not None:
            seen.append(sorted(p.name for p in _home(params).rglob("*")))
        await report({"event": "format", "format_id": chosen})
        return await inner(params, report)

    return step


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("chosen", "finished", "excluded", "next_chosen"),
    [
        ("299+251", (), "299", "298+251"),
        # yt-dlp downloads the streams in the order of the format ids.
        ("299+251", ("299",), "251", "299+140"),
        ("251", (), "251", "140"),
    ],
)
async def test_a_format_googlevideo_refuses_is_excluded_within_the_attempt(
    settings, caplog, chosen, finished, excluded, next_chosen
):
    import logging

    from savetube.ytdl import exclude_formats

    _job_rows(settings, "UPDATE channel SET format = 'bv*[height<=1080]+ba/b[height<=1080]'")
    runner = _Steps(_server_error(chosen, finished=finished), _picks_then_finishes(next_chosen))
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner)
    fmt = "bv*[height<=1080]+ba/b[height<=1080]"
    assert [p["format"] for p in runner.calls] == [fmt, exclude_formats(fmt, [excluded])]
    # One job row and one attempt: no failure, no interruption.
    assert list(jobs) == [1]
    assert (jobs[1].state, jobs[1].attempts) == (JobState.SUCCEEDED, 1)
    assert video.resume_format is None
    [fallback] = _events(caplog, "download.fallback")
    assert fallback.levelno == logging.WARNING
    assert (_fields(fallback)["excluded"], _fields(fallback)["format"]) == (excluded, next_chosen)
    assert _events(caplog, "job.interrupted") == []


@pytest.mark.anyio
async def test_an_hls_copy_of_a_refused_format_is_excluded_too(settings, caplog):
    import logging

    from savetube.ytdl import exclude_formats

    _job_rows(settings, "UPDATE channel SET format = 'bv*+ba/b'")
    # 312 is the HLS copy of 299. Every fragment gets HTTP 500, and yt-dlp
    # skips each one and ends on an empty file.
    runner = _Steps(
        _server_error("299+251"),
        _server_error("312+251", message=EMPTY_FILE),
        _picks_then_finishes("298+251"),
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner)
    assert [p["format"] for p in runner.calls] == [
        "bv*+ba/b",
        exclude_formats("bv*+ba/b", ["299"]),
        exclude_formats("bv*+ba/b", ["299", "312"]),
    ]
    assert list(jobs) == [1]
    assert (jobs[1].state, jobs[1].attempts) == (JobState.SUCCEEDED, 1)
    assert [
        (_fields(e)["excluded"], _fields(e)["format"]) for e in _events(caplog, "download.fallback")
    ] == [("299", "312+251"), ("312", "298+251")]


@pytest.mark.anyio
async def test_the_format_fallback_stops_after_3_exclusions(settings, caplog):
    import logging

    from savetube.ytdl import exclude_formats

    _job_rows(settings, "UPDATE channel SET format = 'bv*+ba/b'")
    runner = _Steps(
        _server_error("299+251"),
        _server_error("298+251"),
        _server_error("135+251"),
        _server_error("134+251"),
        _picks_then_finishes("133+251"),
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner)
    assert [p["format"] for p in runner.calls] == [
        "bv*+ba/b",
        exclude_formats("bv*+ba/b", ["299"]),
        exclude_formats("bv*+ba/b", ["299", "298"]),
        exclude_formats("bv*+ba/b", ["299", "298", "135"]),
    ]
    assert list(jobs) == [1]
    assert (jobs[1].state, jobs[1].log_tail) == (JobState.FAILED, SERVER_500)
    assert [_fields(e)["excluded"] for e in _events(caplog, "download.fallback")] == [
        "299",
        "298",
        "135",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "first",
    [
        # The failing stream had grown, so googlevideo served it before.
        _server_error("299+251", added=1024),
        # An HLS fragment yt-dlp had not appended yet counts too.
        _server_error("312+251", added=1024, suffix=".part-Frag3"),
        # No format was chosen, so no format is known to fail.
        _server_error(None),
    ],
)
async def test_a_server_error_without_a_known_untouched_format_fails_as_before(
    settings, caplog, first
):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'bv*+ba/b'")
    runner = _Steps(first, _picks_then_finishes("298+251"))
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner)
    assert len(runner.calls) == 1
    assert (jobs[1].state, jobs[1].log_tail) == (JobState.FAILED, SERVER_500)
    assert _events(caplog, "download.fallback") == []


@pytest.mark.anyio
async def test_a_resumed_download_falls_back_and_keeps_the_other_parts(settings, caplog):
    import logging

    from savetube.ytdl import exclude_formats

    _job_rows(
        settings,
        "UPDATE channel SET format = 'bv*+ba/b'",
        "UPDATE video SET resume_format = '299+251' WHERE id = 'v1'",
    )
    _part({"paths": {"home": str(settings.incoming_root / "v1")}})
    seen = []
    tools = _FakeTools()
    runner = _Steps(_server_error("299+251"), _picks_then_finishes("298+251", seen))
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner, tools)
    assert [p["format"] for p in runner.calls] == [
        "299+251",
        exclude_formats("bv*+ba/b", ["299"]),
    ]
    assert all(p["continuedl"] for p in runner.calls)
    # The audio part stays for yt-dlp to continue.
    assert "v1.f251.webm.part" in seen[0]
    assert jobs[1].state is JobState.SUCCEEDED
    # The continued part gets the packet pass.
    assert tools.packet_runs == 1
    [fallback] = _events(caplog, "download.fallback")
    assert (_fields(fallback)["excluded"], _fields(fallback)["format"]) == ("299", "298+251")


def _network_rows(count, minutes_ago=10, progress=False):
    """`count` earlier network interruptions of v1's archive job, each with or
    without bytes added."""
    prefix = "network error" if progress else "network error, no progress"
    return [
        "INSERT INTO job (kind, state, video_id, attempts, log_tail, finished_at) VALUES"
        f" ('archive', 'interrupted', 'v1', 1, '{prefix}: x',"
        f" now() - interval '{minutes_ago + i} minutes')"
        for i in range(count)
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "earlier",
    [
        [],
        # Four interruptions so far leave room for a fifth.
        _network_rows(4),
        # A failure ends the run of interruptions before it.
        [
            *_network_rows(5, minutes_ago=60),
            "INSERT INTO job (kind, state, video_id, attempts, log_tail, finished_at) VALUES"
            " ('archive', 'failed', 'v1', 1, 'x', now() - interval '30 minutes')",
        ],
        # A run that added bytes resets the count, however many came before.
        _network_rows(5),
        _network_rows(10, progress=True),
    ],
)
async def test_a_network_error_interrupts_the_archive_job_and_keeps_the_parts(
    settings, caplog, earlier
):
    import logging

    _job_rows(
        settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)", *earlier
    )
    known = set(_job_ids(settings))
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(_network_step()))
    [history] = [j for j in jobs.values() if j.id not in known]
    assert (history.state, history.video_id) == ("interrupted", "v1")
    assert history.log_tail == f"network error: {READ_TIMEOUT}"
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert video.state is VideoState.PENDING and video.resume_format == "251"
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048
    # A network error is no YouTube block, so downloads do not pause.
    assert _block_state(settings) == (0, None)
    assert _events(caplog, "job.failed") == [] and _events(caplog, "dispatch.blocked") == []
    [line] = _events(caplog, "job.interrupted")
    assert line.levelno == logging.WARNING and _fields(line)["reason"] == "network error"
    fields = _fields(line)
    assert list(fields) == [
        *("job", "video", "channel", "title", "took", "present", "added", "reason")
    ]
    assert (fields["present"], fields["added"]) == ("2KiB", "2KiB")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "earlier",
    [
        _network_rows(4),
        # Stalls after the last run that added bytes still count.
        [*_network_rows(4), *_network_rows(1, minutes_ago=60, progress=True)],
    ],
)
async def test_a_network_error_with_no_bytes_added_is_marked_as_a_stall(settings, earlier):
    _job_rows(
        settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)", *earlier
    )
    known = set(_job_ids(settings))
    jobs, _ = await _run_steps(settings, _Steps(_network_step(progress=False)))
    [history] = [j for j in jobs.values() if j.id not in known]
    assert history.log_tail == f"network error, no progress: {READ_TIMEOUT}"
    assert (jobs[1].state, jobs[1].attempts) == (JobState.QUEUED, 0)


@pytest.mark.anyio
async def test_a_sixth_network_stall_in_a_row_fails_the_job(settings, caplog):
    import logging

    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa'",
        "SELECT setval('job_id_seq', 200)",
        *_network_rows(5),
        *_network_rows(3, minutes_ago=60, progress=True),
    )
    known = set(_job_ids(settings))
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(_network_step(progress=False)))
    assert set(jobs) == known
    assert (jobs[1].state, jobs[1].attempts) == (JobState.FAILED, 1)
    assert jobs[1].log_tail == READ_TIMEOUT
    assert video.state is VideoState.FAILED and video.resume_format is None
    assert not (settings.incoming_root / "v1").exists()
    assert _events(caplog, "job.interrupted") == []
    assert len(_events(caplog, "job.failed")) == 1


@pytest.mark.anyio
async def test_a_block_with_a_network_error_text_pauses_downloads(settings, caplog):
    import logging

    message = "ERROR: HTTP Error 429: Too Many Requests. Read timed out."
    _job_rows(settings, "SELECT setval('job_id_seq', 200)")
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _Steps(_network_step(message)))
    [history] = [j for j in jobs.values() if j.id != 1]
    assert history.log_tail == f"blocked by YouTube: {message}"
    assert _block_state(settings)[0] == 1
    [line] = _events(caplog, "job.interrupted")
    assert _fields(line)["reason"] == "blocked by YouTube"


@pytest.mark.anyio
async def test_an_http_403_still_fails_the_archive_job(settings):
    message = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    jobs, video = await _run_steps(settings, _Steps(_network_step(message)))
    assert list(jobs) == [1]
    assert (jobs[1].state, jobs[1].attempts, jobs[1].log_tail) == (JobState.FAILED, 1, message)
    assert video.resume_format is None
    assert not (settings.incoming_root / "v1").exists()


def _job_ids(settings):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        ids = list(c.execute(sa.text("SELECT id FROM job")).scalars())
    engine.dispose()
    return ids
