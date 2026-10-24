"""The format watch of stored lives (docs/plan/live.md, Format watch)."""

import asyncio
import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL, _api, _events, _fields, _queue_row, _sql, _sql_rows, _Steps
from test_formats import HU7_RECORDING, WBJ_RECORDING, _vod

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import FormatWatch, Job, JobKind, JobState, Video

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg or ffprobe not on PATH",
)

DIR = "A [UC1]/lives/2026-10-24 t [v1]"
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def _stored_live(
    settings, *, info=None, video_type="lives", watch_sql="", media=b"recording", watched=True
):
    """Live v1 stored complete in the tree with its recording, and its watch
    unless not `watched`."""
    info = {"id": "v1", "ext": "mkv", "live_status": "is_live", **(info or WBJ_RECORDING)}
    directory = settings.archive_root / DIR
    directory.mkdir(parents=True)
    if media is not None:
        (directory / "v1.mkv").write_bytes(media)
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, live_status, dir_path,"
        " completed_at, format_id, info) VALUES ('v1', 'UC1', :type, 't', 'complete', 'is_live',"
        f" '{DIR}', now(), :fmt, CAST(:info AS jsonb))".replace(":type", f"'{video_type}'")
        .replace(":fmt", f"'{info['format_id']}'")
        .replace(":info", "'" + json.dumps(info).replace("'", "''") + "'"),
        *(
            ["INSERT INTO format_watch (video_id, started_at) VALUES ('v1', now())"]
            if watched
            else []
        ),
        *([watch_sql] if watch_sql else []),
    )
    return directory


class _Tools:
    """ffprobe that reports `duration` for any file."""

    def __init__(self, duration):
        self.duration = duration

    def probe(self, path):
        return {"format": {"duration": str(self.duration)}, "streams": []}


def _probe_vod(video_id="WBJeEquyZ2s", calls=None):
    vod = _vod(video_id)

    def probe(vid, cookiefile):
        if calls is not None:
            calls.append((vid, cookiefile.read_text() if cookiefile else None))
        return {"id": vid, "duration": vod["duration"], "formats": vod["formats"]}

    return probe


async def _check(settings, probe, tools, now=NOW):
    from savetube.watch import check

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    try:
        await check(sessionmaker, settings, "v1", probe=probe, tools=tools, now=now)
        async with sessionmaker() as session:
            watch = await session.get(FormatWatch, "v1")
            jobs = list(await session.scalars(sa.select(Job).order_by(Job.id)))
    finally:
        await engine.dispose()
    return watch, jobs


@pytest.mark.anyio
async def test_a_probe_queues_the_opus_audio_for_a_recording_with_aac(settings):
    _stored_live(settings)

    watch, jobs = await _check(settings, _probe_vod(), _Tools(8311.0))

    assert [(j.kind, j.state, j.format_id) for j in jobs] == [
        (JobKind.REPLACE, JobState.QUEUED, "251")
    ]
    assert (watch.vod_duration, watch.stored_duration) == (8312.0, 8311.0)
    assert watch.checked_at == NOW and watch.probe_error is None
    assert "270" not in {f["format_id"] for f in watch.formats}


@pytest.mark.anyio
async def test_a_duration_difference_over_2_s_holds_the_replace(settings, caplog):
    _stored_live(settings)

    with caplog.at_level(logging.INFO, logger="savetube"):
        watch, jobs = await _check(settings, _probe_vod(), _Tools(8309.5))

    assert jobs == []
    assert len(_events(caplog, "watch.held")) == 1
    assert watch.formats


@pytest.mark.anyio
async def test_a_manual_pick_ends_the_automatic_replaces(settings):
    _stored_live(settings, watch_sql="UPDATE format_watch SET manual = true")

    _, jobs = await _check(settings, _probe_vod(), _Tools(8312.0))

    assert jobs == []


@pytest.mark.anyio
async def test_a_queued_replace_gets_no_second(settings):
    _stored_live(
        settings,
        watch_sql="INSERT INTO job (kind, state, video_id, format_id, attempts)"
        " VALUES ('replace', 'queued', 'v1', '251', 0)",
    )

    _, jobs = await _check(settings, _probe_vod(), _Tools(8312.0))

    assert len(jobs) == 1


@pytest.mark.anyio
async def test_a_failed_probe_is_stored_and_queues_nothing(settings):
    _stored_live(settings)

    def gone(video_id, cookiefile):
        raise RuntimeError("ERROR: [youtube] v1: This live stream recording is not available.")

    watch, jobs = await _check(settings, gone, _Tools(8312.0))

    assert jobs == []
    assert "not available" in watch.probe_error and watch.checked_at == NOW


@pytest.mark.anyio
async def test_a_members_live_is_probed_with_the_member_profile(settings, tmp_path):
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    (cookies / "main.txt").write_text("member cookies")
    (cookies / "general.txt").write_text("general cookies")
    member = settings.model_copy(update={"cookies_dir": cookies})
    _stored_live(
        settings,
        info=HU7_RECORDING,
        video_type="members",
        watch_sql="INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id)"
        " VALUES ('UC1', 'main', true, now(), 'm1')",
    )
    _sql(settings, "UPDATE download_settings SET cookie_profile = 'general'")
    calls = []

    _, jobs = await _check(member, _probe_vod("hU7U_HnNWUo", calls), _Tools(7421.0))

    assert calls == [("v1", "member cookies")]
    # 251, not 251-drc. The VOD's 299 is H.264 against the stored VP9, so the
    # video stays.
    assert [j.format_id for j in jobs] == ["251"]


@pytest.mark.anyio
async def test_only_due_watches_are_probed(settings):
    from savetube.watch import watch_formats

    _stored_live(settings)
    _sql(
        settings,
        "INSERT INTO video (id, channel_id, type, title, state, live_status, info) VALUES"
        " ('fresh', 'UC1', 'lives', 't', 'complete', 'was_live', '{}'),"
        " ('old', 'UC1', 'lives', 't', 'complete', 'was_live', '{}')",
        # Checked an hour ago, and started 8 days ago.
        "INSERT INTO format_watch (video_id, started_at, checked_at) VALUES"
        f" ('fresh', '{NOW - timedelta(days=1)}', '{NOW - timedelta(hours=1)}'),"
        f" ('old', '{NOW - timedelta(days=8)}', null)",
        f"UPDATE format_watch SET checked_at = '{NOW - timedelta(hours=7)}' WHERE video_id = 'v1'",
    )
    calls = []
    engine = make_engine(settings.sqlalchemy_url)
    await watch_formats(
        make_sessionmaker(engine),
        settings,
        probe=_probe_vod(calls=calls),
        tools=_Tools(8312.0),
        lock=asyncio.Lock(),
        now=NOW,
    )
    await engine.dispose()

    assert [vid for vid, _ in calls] == ["v1"]
    ended = dict(_sql_rows(settings, "SELECT video_id, ended_at IS NOT NULL FROM format_watch"))
    assert ended == {"v1": False, "fresh": False, "old": True}


async def _end(settings, now):
    from savetube.watch import end_watches

    engine = make_engine(settings.sqlalchemy_url)
    await end_watches(make_sessionmaker(engine), settings, now)
    await engine.dispose()


@pytest.mark.anyio
async def test_the_end_of_a_watch_deletes_the_original_a_replace_kept(settings):
    directory = _stored_live(
        settings,
        media=b"replaced",
        watch_sql="UPDATE format_watch SET original_path = '.originals/v1/v1.mkv'",
    )
    aside = settings.archive_root / ".originals" / "v1" / "v1.mkv"
    aside.parent.mkdir(parents=True)
    aside.write_bytes(b"recording")

    await _end(settings, datetime.now(UTC) + timedelta(days=6))
    assert aside.exists()

    await _end(settings, datetime.now(UTC) + timedelta(days=7, minutes=1))
    assert not aside.parent.exists()
    assert (directory / "v1.mkv").read_bytes() == b"replaced"
    [(ended,)] = _sql_rows(settings, "SELECT ended_at IS NOT NULL FROM format_watch")
    assert ended


@pytest.mark.anyio
async def test_a_watch_without_a_replace_ends_with_the_recording_in_place(settings):
    directory = _stored_live(settings)

    await _end(settings, datetime.now(UTC) + timedelta(days=8))

    assert (directory / "v1.mkv").read_bytes() == b"recording"
    [(ended,)] = _sql_rows(settings, "SELECT ended_at IS NOT NULL FROM format_watch")
    assert ended


@pytest.mark.anyio
async def test_a_watch_waits_for_its_running_replace_before_it_ends(settings):
    _stored_live(
        settings,
        watch_sql="INSERT INTO job (kind, state, video_id, format_id, attempts)"
        " VALUES ('replace', 'running', 'v1', '251', 0)",
    )

    await _end(settings, datetime.now(UTC) + timedelta(days=8))

    [(ended,)] = _sql_rows(settings, "SELECT ended_at IS NOT NULL FROM format_watch")
    assert not ended


# Manual probes ----------------------------------------------------------------


def _never_probe(video_id, cookiefile):
    pytest.fail(f"no probe of {video_id} expected")


async def _probe_now(settings, probe, now=NOW):
    from savetube.watch import probe_now

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    try:
        await probe_now(
            sessionmaker,
            settings,
            "v1",
            probe=probe,
            tools=_Tools(8312.0),
            lock=asyncio.Lock(),
            now=now,
        )
        async with sessionmaker() as session:
            watch = await session.get(FormatWatch, "v1")
            jobs = list(await session.scalars(sa.select(Job).order_by(Job.id)))
    finally:
        await engine.dispose()
    return watch, jobs


async def _watch_loop(settings, now, probe=_never_probe):
    from savetube.watch import watch_formats

    engine = make_engine(settings.sqlalchemy_url)
    await watch_formats(
        make_sessionmaker(engine),
        settings,
        probe=probe,
        tools=_Tools(8312.0),
        lock=asyncio.Lock(),
        now=now,
    )
    await engine.dispose()


@pytest.mark.anyio
async def test_a_manual_probe_makes_a_row_the_watch_loop_never_probes_or_replaces(settings):
    # The stored AAC has an Opus upgrade, which an open watch queues.
    _stored_live(settings, video_type="videos", watched=False)

    watch, jobs = await _probe_now(settings, _probe_vod())

    assert watch.started_at is None and watch.checked_at == NOW
    assert "251" in {f["format_id"] for f in watch.formats}
    assert jobs == []

    # 7 hours on, a watch would be due for its next probe.
    await _watch_loop(settings, NOW + timedelta(hours=7))

    rows = _sql_rows(settings, f"SELECT ended_at, checked_at = '{NOW}' FROM format_watch")
    assert rows == [(None, True)]
    assert _sql_rows(settings, "SELECT id FROM job") == []


@pytest.mark.anyio
async def test_a_manual_probe_of_an_open_watch_counts_as_its_probe(settings):
    _stored_live(settings)

    watch, jobs = await _probe_now(settings, _probe_vod())

    assert watch.checked_at == NOW
    assert [(j.format_id, j.manual) for j in jobs] == [("251", False)]


@pytest.mark.anyio
async def test_a_manual_probe_of_an_ended_watch_refreshes_the_formats_only(settings):
    ended = NOW - timedelta(days=1)
    _stored_live(
        settings,
        watch_sql=f"UPDATE format_watch SET started_at = '{NOW - timedelta(days=9)}',"
        f" ended_at = '{ended}'",
    )

    watch, jobs = await _probe_now(settings, _probe_vod())

    assert watch.formats and watch.checked_at == NOW and watch.ended_at == ended
    assert jobs == []


@pytest.mark.anyio
async def test_a_manual_probe_refuses_a_video_not_complete(settings):
    from savetube.watch import NotComplete

    _stored_live(settings, watched=False)
    _sql(settings, "UPDATE video SET state = 'staged'")

    with pytest.raises(NotComplete):
        await _probe_now(settings, _never_probe)

    assert _sql_rows(settings, "SELECT video_id FROM format_watch") == []


def _kept_original(settings, *, watch_sql):
    directory = _stored_live(settings, media=b"replaced", watch_sql=watch_sql)
    aside = settings.archive_root / ".originals" / "v1" / "v1.mkv"
    aside.parent.mkdir(parents=True)
    aside.write_bytes(b"recording")
    return directory, aside


@pytest.mark.anyio
async def test_the_original_of_a_manual_swap_goes_7_days_after_the_swap(settings):
    until = NOW + timedelta(days=7)
    directory, aside = _kept_original(
        settings,
        watch_sql="UPDATE format_watch SET started_at = NULL,"
        f" original_path = '.originals/v1/v1.mkv', original_until = '{until}'",
    )

    await _watch_loop(settings, until - timedelta(minutes=1))
    assert aside.read_bytes() == b"recording"

    await _watch_loop(settings, until + timedelta(minutes=1))
    assert not aside.parent.exists()
    assert (directory / "v1.mkv").read_bytes() == b"replaced"
    assert _sql_rows(settings, "SELECT original_path, original_until FROM format_watch") == [
        (None, None)
    ]


@pytest.mark.anyio
async def test_the_end_of_a_watch_keeps_an_original_with_its_own_7_days(settings):
    # A pick that ran after the watch's 7 days swapped with a keep of its own.
    until = datetime.now(UTC) + timedelta(days=6)
    _, aside = _kept_original(
        settings,
        watch_sql="UPDATE format_watch SET original_path = '.originals/v1/v1.mkv',"
        f" original_until = '{until}'",
    )

    await _end(settings, datetime.now(UTC) + timedelta(days=7, minutes=1))

    assert aside.read_bytes() == b"recording"
    [(ended,)] = _sql_rows(settings, "SELECT ended_at IS NOT NULL FROM format_watch")
    assert ended


@pytest.mark.anyio
async def test_a_finished_recording_starts_a_watch(settings):
    from savetube.jobs import run

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'lives', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts)"
        " VALUES (1, 'record', 'queued', 'v1', 0)",
    )

    aired = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)

    async def record(params, report):
        home = Path(params["paths"]["home"])
        (home / "v1.mkv").write_bytes(b"recording")
        media = home / "v1.mkv"
        return {"id": "v1", "title": "t", "channel_id": "UC1", "live_status": "is_live",
                "release_timestamp": int(aired.timestamp()), "ext": "mkv",
                "requested_downloads": [{"filepath": str(media)}]}  # fmt: skip

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await run(1, settings, live=True, runner=_Steps(record), sessionmaker=sessionmaker)
    async with sessionmaker() as session:
        watch = await session.get(FormatWatch, "v1")
    await engine.dispose()

    assert watch is not None and watch.started_at == aired


@pytest.mark.anyio
async def test_a_recording_stores_the_measured_size_of_each_stream(settings, monkeypatch):
    from savetube.jobs import run, runners

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'lives', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts)"
        " VALUES (1, 'record', 'queued', 'v1', 0)",
    )

    def download(url, params, on_format):
        home = Path(params["paths"]["home"])
        [hook] = params["progress_hooks"]
        on_format({"format_id": "299+140"})
        for format_id, data in (("299", b"video stream"), ("140", b"audio")):
            stream = home / f"v1.f{format_id}"
            stream.write_bytes(data)
            hook({"status": "finished", "filename": str(stream),
                  "info_dict": {"format_id": format_id}})  # fmt: skip
        media = home / "v1.mkv"
        media.write_bytes(b"merged")
        return {"id": "v1", "title": "t", "channel_id": "UC1", "live_status": "is_live",
                "ext": "mkv", "format_id": "299+140",
                "requested_formats": [
                    {"format_id": "299", "vcodec": "avc1", "acodec": "none", "tbr": 4600.0},
                    {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128.0},
                ],
                "requested_downloads": [{"filepath": str(media)}]}  # fmt: skip

    monkeypatch.setattr(runners, "download", download)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await run(1, settings, live=True, runner=runners.in_process, sessionmaker=sessionmaker)
    async with sessionmaker() as session:
        video = await session.get(Video, "v1")
    await engine.dispose()

    assert video.state.value == "complete"
    assert [(f["format_id"], f.get("filesize")) for f in video.info["requested_formats"]] == [
        ("299", len(b"video stream")),
        ("140", len(b"audio")),
    ]


async def _record_media(settings, write, caplog=None):
    """Record v1 with a runner that writes the recording with `write`."""
    from savetube.jobs import run

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'lives', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts)"
        " VALUES (1, 'record', 'queued', 'v1', 0)",
    )

    async def record(params, report):
        media = Path(params["paths"]["home"]) / "v1.mkv"
        write(media)
        return {"id": "v1", "title": "t", "channel_id": "UC1", "live_status": "is_live",
                "ext": "mkv", "vcodec": "mpeg4", "acodec": "opus",
                "requested_downloads": [{"filepath": str(media)}]}  # fmt: skip

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await run(1, settings, live=True, runner=_Steps(record), sessionmaker=sessionmaker)
    async with sessionmaker() as session:
        video = await session.get(Video, "v1")
        job = await session.get(Job, 1)
    await engine.dispose()
    return video, job


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_finished_recording_is_verified_and_gets_its_audio_copy(settings):
    from test_audio import make_media

    video, job = await _record_media(settings, make_media)

    assert job.state is JobState.SUCCEEDED
    assert video.verified_at is not None
    assert video.verified_checks == ["probe", "packets"]
    directory = settings.archive_root / video.dir_path
    assert sorted(p.name for p in directory.iterdir()) == ["v1.mkv", "v1.opus"]
    assert video.size_bytes == sum(p.stat().st_size for p in directory.iterdir())


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_recording_that_fails_its_checks_is_stored_unverified(settings, caplog):
    with caplog.at_level(logging.INFO, logger="savetube"):
        video, job = await _record_media(settings, lambda p: p.write_bytes(b"recording"))

    assert job.state is JobState.SUCCEEDED
    assert video.state.value == "complete" and video.verified_at is None
    media = settings.archive_root / video.dir_path / "v1.mkv"
    assert media.read_bytes() == b"recording"
    assert [_fields(r)["check"] for r in _events(caplog, "record.integrity_failed")] == ["probe"]


@pytest.mark.anyio
async def test_a_recording_whose_check_errors_is_stored_unverified(settings, monkeypatch):
    from savetube import integrity

    def broken(media, info, tools):
        raise OSError("ffprobe crashed")

    monkeypatch.setattr(integrity, "verify_recording", broken)

    video, job = await _record_media(settings, lambda p: p.write_bytes(b"recording"))

    assert job.state is JobState.SUCCEEDED and video.verified_at is None
    assert (settings.archive_root / video.dir_path / "v1.mkv").read_bytes() == b"recording"


@pytest.mark.anyio
async def test_an_archive_download_that_is_no_live_starts_no_watch(settings):
    from conftest import _finished, _job_rows, _run_steps

    _job_rows(settings)

    _, video = await _run_steps(settings, _Steps(_finished()))

    assert video.state.value == "complete"
    assert _sql_rows(settings, "SELECT video_id FROM format_watch") == []


async def _move_staged_vod(settings, *, upload_date, info):
    """Moves the staged VOD s1 of a live into the tree. Returns its watch's
    start and end, or None without a watch."""
    from savetube.staging import move_staged
    from savetube.watch import ends_at

    staged = settings.model_copy(update={"staging_root": settings.archive_root.parent / "staging"})
    ready = staged.staging_root / "ready" / "s1"
    ready.mkdir(parents=True)
    (ready / "s1.mkv").write_bytes(b"x")
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, live_status, dir_path,"
        " size_bytes, completed_at, upload_date, info) VALUES ('s1', 'UC1', 'lives', 't',"
        " 'staged', 'was_live', 'A [UC1]/lives/s1', 1, now() - interval '2 hours',"
        f" {upload_date}, CAST('{json.dumps(info)}' AS jsonb))",
    )
    assert _sql_rows(settings, "SELECT video_id FROM format_watch") == []

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await move_staged(sessionmaker, staged, requested=True)
    async with sessionmaker() as session:
        watch = await session.get(FormatWatch, "s1")
    await engine.dispose()
    return watch and (watch.started_at, ends_at(watch))


@pytest.mark.anyio
async def test_a_staged_vod_of_a_live_aired_2_days_ago_is_watched_5_more_days(settings):
    aired = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)

    watch = await _move_staged_vod(
        settings,
        upload_date=f"'{aired.date().isoformat()}'",
        info={"release_timestamp": int(aired.timestamp()), "timestamp": 1},
    )

    assert watch == (aired, aired + timedelta(days=7))


@pytest.mark.anyio
async def test_a_live_without_release_timestamp_airs_at_its_timestamp(settings):
    aired = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)

    watch = await _move_staged_vod(
        settings, upload_date="NULL", info={"timestamp": int(aired.timestamp())}
    )

    assert watch == (aired, aired + timedelta(days=7))


@pytest.mark.anyio
async def test_a_live_with_only_an_upload_date_airs_at_its_midnight_utc(settings):
    day = (datetime.now(UTC) - timedelta(days=3)).date()

    watch = await _move_staged_vod(settings, upload_date=f"'{day.isoformat()}'", info={})

    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    assert watch == (midnight, midnight + timedelta(days=7))


@pytest.mark.anyio
async def test_a_backfilled_live_aired_long_ago_gets_no_watch(settings):
    long_ago = int(datetime(2024, 5, 1, 12, tzinfo=UTC).timestamp())

    watch = await _move_staged_vod(
        settings, upload_date="'2024-05-01'", info={"release_timestamp": long_ago}
    )

    assert watch is None


def test_migration_0020_watches_the_lives_stored_in_the_last_7_days(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    config = alembic_config(settings.sqlalchemy_url)
    command.downgrade(config, "0019")
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, live_status, completed_at) VALUES"
        " ('new', 'UC1', 'lives', 't', 'complete', 'is_live', now() - interval '2 days'),"
        " ('vod', 'UC1', 'lives', 't', 'complete', 'was_live', now() - interval '6 days'),"
        " ('aged', 'UC1', 'lives', 't', 'complete', 'was_live', now() - interval '8 days'),"
        " ('plain', 'UC1', 'videos', 't', 'complete', 'not_live', now()),"
        " ('wait', 'UC1', 'lives', 't', 'staged', 'was_live', now())",
    )

    command.upgrade(config, "0020")

    rows = _sql_rows(
        settings,
        "SELECT w.video_id, w.started_at = v.completed_at FROM format_watch w"
        " JOIN video v ON v.id = w.video_id ORDER BY 1",
    )
    assert rows == [("new", True), ("vod", True)]


def _info(**fields):
    return "CAST('" + json.dumps(fields) + "' AS jsonb)"


def test_migration_0021_ends_the_watches_of_lives_aired_over_7_days_ago(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    config = alembic_config(settings.sqlalchemy_url)
    command.downgrade(config, "0020")
    now = datetime.now(UTC)
    recent = int((now - timedelta(days=2)).timestamp())
    old = int((now - timedelta(days=400)).timestamp())
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, live_status, completed_at,"
        " upload_date, info) VALUES"
        f" ('recent', 'UC1', 'lives', 't', 'complete', 'is_live', now(), NULL,"
        f"  {_info(release_timestamp=recent)}),"
        f" ('old', 'UC1', 'lives', 't', 'complete', 'was_live', now(), NULL,"
        f"  {_info(release_timestamp=old, timestamp=recent)}),"
        " ('dated', 'UC1', 'lives', 't', 'complete', 'was_live', now(), '2025-01-02', NULL),"
        f" ('busy', 'UC1', 'lives', 't', 'complete', 'was_live', now(), NULL,"
        f"  {_info(timestamp=old)}),"
        f" ('kept', 'UC1', 'lives', 't', 'complete', 'was_live', now(), NULL,"
        f"  {_info(timestamp=old)}),"
        f" ('stale', 'UC1', 'lives', 't', 'complete', 'was_live', now(), NULL,"
        f"  {_info(timestamp=old)}),"
        f" ('picked', 'UC1', 'lives', 't', 'complete', 'was_live', now(), NULL,"
        f"  {_info(timestamp=recent)})",
        "INSERT INTO format_watch (video_id, started_at, original_path, manual) VALUES"
        " ('recent', now(), NULL, false), ('old', now(), NULL, false),"
        " ('dated', now(), NULL, false), ('busy', now(), NULL, false),"
        " ('kept', now(), '.originals/kept/kept.mkv', false), ('stale', now(), NULL, true),"
        " ('picked', now(), NULL, true)",
    )
    dispatched = _queue_row(settings)
    _sql(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts, queue_job_id) VALUES"
        " (1, 'replace', 'running', 'busy', 1, NULL),"
        " (2, 'replace', 'queued', 'stale', 0, NULL),"
        f" (3, 'replace', 'queued', 'recent', 0, {dispatched}),"
        " (4, 'replace', 'queued', 'picked', 0, NULL)",
    )
    originals = settings.archive_root / ".originals" / "kept"
    originals.mkdir(parents=True)
    (originals / "kept.mkv").write_bytes(b"recording")

    command.upgrade(config, "head")

    rows = _sql_rows(
        settings,
        "SELECT video_id, extract(epoch FROM started_at)::bigint,"
        " ended_at IS NOT NULL FROM format_watch ORDER BY 1",
    )
    jan_2 = int(datetime(2025, 1, 2, tzinfo=UTC).timestamp())
    assert rows == [
        ("busy", old, False),
        ("dated", jan_2, True),
        ("kept", old, False),
        ("old", old, True),
        ("picked", recent, False),
        ("recent", recent, False),
        ("stale", old, True),
    ]
    assert (originals / "kept.mkv").read_bytes() == b"recording"
    jobs = _sql_rows(settings, "SELECT id, state, finished_at IS NOT NULL FROM job ORDER BY id")
    # An old watch's queued replace and a recent automatic one are cancelled.
    # A recent manual pick waits.
    assert jobs == [
        (1, "running", False),
        (2, "cancelled", True),
        (3, "cancelled", True),
        (4, "queued", False),
    ]
    assert _sql_rows(settings, f"SELECT id FROM procrastinate_jobs WHERE id = {dispatched}") == []


@pytest.mark.anyio
async def test_the_dispatcher_sends_a_queued_replace(settings):
    from savetube.jobs import dispatch

    _stored_live(
        settings,
        watch_sql="INSERT INTO job (id, kind, state, video_id, format_id, attempts)"
        " VALUES (7, 'replace', 'queued', 'v1', '251', 0)",
    )
    sent = []

    async def enqueue(job_id):
        sent.append(job_id)
        return 900

    engine = make_engine(settings.sqlalchemy_url)
    async with make_sessionmaker(engine)() as session:
        d = await dispatch(session, datetime.now(UTC), enqueue)
    await engine.dispose()

    assert sent == [7] and d.started == [7]


# The API ------------------------------------------------------------------


def _watched_with_formats(settings, *, stored_duration=8312.0, info=None):
    from savetube.formats import counted

    _stored_live(settings, info=info)
    vod = _vod("WBJeEquyZ2s")
    _sql(
        settings,
        "UPDATE format_watch SET checked_at = now(), vod_duration = 8312,"
        f" stored_duration = {stored_duration}, formats = CAST('"
        + json.dumps(counted(vod["formats"])).replace("'", "''")
        + "' AS jsonb)",
    )


def test_api_marks_the_pick_and_the_kept_stream_recommended(settings):
    _watched_with_formats(settings)

    with _api(settings) as api:
        body = api.get("/api/videos/v1/formats").json()

    recommended = [
        (r["format_id"], r["stored"], r["reason"])
        for r in body["stored"] + body["formats"]
        if r["recommended"]
    ]
    assert sorted(recommended) == [
        ("137", True, "the stored H.264 is this format"),
        ("251", False, "Opus over the stored AAC"),
    ]
    assert body["held"] is False and body["duration_diff"] == 0.0
    assert {r["format_id"] for r in body["formats"]} >= {"140", "251", "137", "243"}


def test_api_answers_null_for_a_video_without_a_watch(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v2', 'UC1', 'videos', 't', 'complete')",
    )

    with _api(settings) as api:
        r = api.get("/api/videos/v2/formats")

    assert r.status_code == 200 and r.json() is None


def test_api_pick_queues_a_replace_and_ends_the_automatic_ones(settings):
    _watched_with_formats(settings)
    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["243"]})
        again = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["251"]})

    assert r.status_code == 200
    assert r.json()["manual"] is True and r.json()["replace"]["format_id"] == "243"
    rows = _sql_rows(settings, "SELECT kind, state, format_id FROM job")
    assert rows == [("replace", "queued", "243")]
    assert again.status_code == 409


def test_api_pick_of_a_held_video_needs_a_confirm(settings):
    _watched_with_formats(settings, stored_duration=8300.0)
    with _api(settings) as api:
        refused = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["251"]})
        confirmed = api.post(
            "/api/videos/v1/formats/pick", json={"format_ids": ["251"], "confirm": True}
        )

    assert refused.status_code == 409 and "12.0 s" in refused.json()["detail"]
    assert confirmed.status_code == 200 and confirmed.json()["held"] is True


def test_api_stored_rows_without_a_size_estimate_it_from_bitrate_and_duration(settings):
    # WBJ_RECORDING's streams carry no size, as a recording made before the
    # recorder measured its streams. Stored for 8312 s.
    _watched_with_formats(settings)

    with _api(settings) as api:
        body = api.get("/api/videos/v1/formats").json()

    sizes = {r["format_id"]: (r["size"], r["size_estimated"]) for r in body["stored"]}
    # 1100 kbps × 8312 s / 8 and 128 kbps × 8312 s / 8.
    assert sizes == {"137": (1_142_900_000, True), "140": (132_992_000, True)}
    assert all(r["size_estimated"] is False for r in body["formats"])


def test_api_stored_rows_show_the_size_the_info_dict_holds(settings):
    info = {
        "format_id": "137+140",
        "requested_formats": [
            {**WBJ_RECORDING["requested_formats"][0], "filesize": 1_234_567},
            {**WBJ_RECORDING["requested_formats"][1], "filesize_approx": 98_765},
        ],
    }
    _stored_live(settings, info=info, watch_sql="UPDATE format_watch SET stored_duration = 8312")

    with _api(settings) as api:
        body = api.get("/api/videos/v1/formats").json()

    sizes = {r["format_id"]: (r["size"], r["size_estimated"]) for r in body["stored"]}
    assert sizes == {"137": (1_234_567, False), "140": (98_765, False)}


def test_api_stored_row_without_a_duration_has_no_size(settings):
    _stored_live(settings)

    with _api(settings) as api:
        body = api.get("/api/videos/v1/formats").json()

    assert [(r["size"], r["size_estimated"]) for r in body["stored"]] == [(None, False)] * 2


@pytest.mark.parametrize(
    ("format_ids", "queued"),
    [(["137"], "137"), (["140", "137"], "140+137")],
    ids=["video", "video-and-audio"],
)
def test_api_pick_of_the_stored_format_id_queues_a_manual_replace(settings, format_ids, queued):
    # A live recording and its VOD share an itag but are different encodes.
    _watched_with_formats(settings)

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_ids": format_ids})

    assert r.status_code == 200, r.text
    assert _sql_rows(settings, "SELECT format_id, manual FROM job") == [(queued, True)]


def _stored_with(video, audio):
    """Stored streams 137 and 140 of WBJ_RECORDING with these extra keys."""
    v, a = WBJ_RECORDING["requested_formats"]
    return {"format_id": f"{video.get('format_id', '137')}+140",
            "requested_formats": [{**v, **video}, {**a, **audio}]}  # fmt: skip


def _rows(body):
    return (
        [(r["format_id"], r["recorded"]) for r in body["stored"]],
        [(r["format_id"], r["recommended"]) for r in body["formats"] if r["is_stored"]],
    )


def test_api_a_stored_youtube_stream_has_no_stored_row_but_its_youtube_row(settings):
    https = {"protocol": "https"}
    _watched_with_formats(settings, info=_stored_with(https, https))

    with _api(settings) as api:
        body = api.get("/api/videos/v1/formats").json()
        pick = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["137"]})

    # 137 is the recommendation: the stored H.264 is this format. 251 beats 140.
    assert _rows(body) == ([], [("137", True), ("140", False)])
    [video] = [r for r in body["formats"] if r["format_id"] == "137"]
    assert video["reason"] == "the stored H.264 is this format"
    # Picking it would download the stored stream again.
    assert pick.status_code == 422
    assert _sql_rows(settings, "SELECT id FROM job") == []


def test_api_a_recorded_stream_keeps_its_stored_row_beside_its_youtube_row(settings):
    dash = {"protocol": "http_dash_segments_generator"}
    _watched_with_formats(settings, info=_stored_with(dash, dash))

    with _api(settings) as api:
        body = api.get("/api/videos/v1/formats").json()

    assert _rows(body) == ([("137", True), ("140", True)], [])
    assert {"137", "140"} <= {r["format_id"] for r in body["formats"]}


def test_api_a_stored_youtube_stream_the_probe_does_not_list_keeps_its_stored_row(settings):
    https = {"protocol": "https"}
    _watched_with_formats(settings, info=_stored_with({**https, "format_id": "999"}, https))

    with _api(settings) as api:
        body = api.get("/api/videos/v1/formats").json()

    assert _rows(body) == ([("999", False)], [("140", False)])


def test_api_pick_refuses_a_format_the_vod_does_not_offer(settings):
    _watched_with_formats(settings)

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["251-drc"]})

    assert r.status_code == 404
    assert _sql_rows(settings, "SELECT id FROM job") == []


def test_api_pick_after_the_watch_ended_queues_a_manual_replace(settings):
    _watched_with_formats(settings)
    _sql(
        settings,
        "UPDATE format_watch SET started_at = now() - interval '9 days',"
        " ended_at = now() - interval '2 days'",
    )

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["251"]})

    assert r.status_code == 200 and r.json()["ended_at"] is not None
    assert _sql_rows(settings, "SELECT format_id, manual FROM job") == [("251", True)]


def _replace_row(settings, *, state="queued", manual=False, queue_job_id=None):
    queue = "NULL" if queue_job_id is None else str(queue_job_id)
    _sql(
        settings,
        "INSERT INTO job (id, kind, state, video_id, format_id, attempts, manual, queue_job_id)"
        f" VALUES (40, 'replace', '{state}', 'v1', '251', 0, {manual}, {queue})",
        # A later job gets a later id, as in production.
        "SELECT setval(pg_get_serial_sequence('job', 'id'), 40)",
    )


def test_api_pick_of_video_and_audio_supersedes_a_queued_automatic_replace(settings, caplog):
    _watched_with_formats(settings)
    queue_job_id = _queue_row(settings)
    _replace_row(settings, queue_job_id=queue_job_id)

    with caplog.at_level(logging.INFO, logger="savetube"), _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["243", "251"]})

    assert r.status_code == 200, r.text
    assert r.json()["replace"]["format_id"] == "251+243"
    rows = _sql_rows(
        settings,
        "SELECT id, state, format_id, manual, finished_at IS NOT NULL FROM job ORDER BY id",
    )
    assert rows[0] == (40, "cancelled", "251", False, True)
    assert rows[1][1:] == ("queued", "251+243", True, False)
    assert _sql_rows(
        settings, f"SELECT status::text FROM procrastinate_jobs WHERE id = {queue_job_id}"
    ) == [("cancelled",)]
    assert [_fields(r)["job"] for r in _events(caplog, "job.cancelled")] == [40]


def test_api_pick_supersedes_a_queued_automatic_replace_the_dispatcher_has_not_sent(settings):
    _watched_with_formats(settings)
    _replace_row(settings)

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["251"]})

    assert r.status_code == 200, r.text
    assert _sql_rows(settings, "SELECT state, manual FROM job ORDER BY id") == [
        ("cancelled", False),
        ("queued", True),
    ]


@pytest.mark.parametrize(
    ("state", "manual", "queue_status"),
    [("running", False, None), ("queued", True, None), ("queued", False, "doing")],
    ids=["running", "queued-manual", "queued-automatic-taken-by-a-worker"],
)
def test_api_pick_refuses_while_a_replace_runs_or_a_pick_waits(
    settings, state, manual, queue_status
):
    _watched_with_formats(settings)
    queue_job_id = None if queue_status is None else _queue_row(settings, status=queue_status)
    _replace_row(settings, state=state, manual=manual, queue_job_id=queue_job_id)

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["251"]})

    assert r.status_code == 409
    assert _sql_rows(settings, "SELECT id, state FROM job") == [(40, state)]


@pytest.mark.parametrize(
    "format_ids",
    [["243", "136"], ["251", "251"], [], ["243", "251", "250"]],
    ids=["two-video", "same-twice", "none", "three"],
)
def test_api_pick_refuses_a_bad_set_of_formats(settings, format_ids):
    _watched_with_formats(settings)

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_ids": format_ids})

    assert r.status_code == 422
    assert _sql_rows(settings, "SELECT id FROM job") == []


def test_api_lists_the_formats_best_first(settings):
    # The probe lists them as yt-dlp does, worst first.
    _watched_with_formats(settings)

    with _api(settings) as api:
        body = api.get("/api/videos/v1/formats").json()

    ids = {
        k: [r["format_id"] for r in body["formats"] if r["kind"] == k] for k in ("video", "audio")
    }
    # Height, then fps, then AV1 > VP9 > H.264: the 360p VP9 above the 360p H.264.
    assert ids["video"] == ["137", "136", "135", "243", "134", "133", "160"]
    # Opus over AAC, then abr.
    assert ids["audio"] == ["251", "250", "249", "140", "139"]


class _Scheduler:
    """The scheduler's API behind the web's outbound httpx2 calls."""

    def __init__(self, settings, probe):
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from savetube.db import PSYCOPG_CONNECT_ARGS
        from savetube.scheduler_api import Requests, create_app

        # NullPool: the web app's loop runs these calls, so no connection
        # outlives it.
        engine = create_async_engine(
            settings.sqlalchemy_url, connect_args=PSYCOPG_CONNECT_ARGS, poolclass=NullPool
        )
        self.app = create_app(
            settings,
            make_sessionmaker(engine),
            probes=asyncio.Lock(),
            probe=probe,
            tools=_Tools(8312.0),
            requests=Requests(),
        )
        self.urls = []

    async def handle(self, request):
        self.urls.append(str(request.url))
        return await httpx2.ASGITransport(app=self.app).handle_async_request(request)


def test_api_probe_then_pick_on_a_video_without_a_watch(settings, monkeypatch):
    _stored_live(settings, video_type="videos", watched=False)
    scheduler = _Scheduler(settings, _probe_vod())
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)

    with _api(settings) as api:
        assert api.get("/api/videos/v1/formats").json() is None
        probed = api.post("/api/videos/v1/formats/probe")
        picked = api.post("/api/videos/v1/formats/pick", json={"format_ids": ["251"]})

    assert scheduler.urls == [f"{settings.scheduler_url}/probe/v1"]
    assert probed.status_code == 200, probed.text
    body = probed.json()
    assert body["started_at"] is None and body["ends_at"] is None
    assert body["checked_at"] is not None and body["replace"] is None
    assert [r["format_id"] for r in body["formats"] if r["recommended"]] == ["251"]
    assert picked.status_code == 200
    assert _sql_rows(settings, "SELECT format_id, manual FROM job") == [("251", True)]


def test_api_probe_refuses_a_video_not_complete_without_calling_the_scheduler(
    settings, monkeypatch
):
    _stored_live(settings, watched=False)
    _sql(settings, "UPDATE video SET state = 'downloading'")
    scheduler = _Scheduler(settings, _never_probe)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/probe")

    assert r.status_code == 409 and scheduler.urls == []


def test_api_probe_answers_504_when_the_scheduler_times_out(settings, monkeypatch):
    _stored_live(settings, watched=False)

    async def slow(self, request):
        raise httpx2.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", slow)

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/probe")

    assert r.status_code == 504 and "120 s" in r.json()["detail"]


@pytest.mark.anyio
@pytest.mark.parametrize(("manual", "first"), [(True, 20), (False, 12)])
async def test_a_manual_replace_dispatches_before_waiting_archives(settings, manual, first):
    from conftest import _dispatch, _waiting_rows

    # One archive runs, so max_concurrent 2 leaves one slot.
    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 2",
        "INSERT INTO job (id, kind, state, video_id, format_id, attempts, manual) VALUES"
        f" (20, 'replace', 'queued', 'c', '251', 0, {manual})",
    )

    enqueued, _ = await _dispatch(settings)

    assert enqueued == [first]


# The replace job ------------------------------------------------------------


def _ffprobe_streams(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [(s["codec_type"], s["codec_name"]) for s in json.loads(out.stdout)["streams"]]


def _opus_download(source):
    """A runner step that downloads `source`, an Opus audio-only file, as 251."""

    async def step(params, report):
        # The job made its working directory.
        media = Path(params["paths"]["home"]) / "v1.webm"
        shutil.copyfile(source, media)
        size = media.stat().st_size
        await report({"event": "stream", "format_id": "251", "size": size, "expected": size})
        return {
            "id": "v1",
            "duration": 1,
            "format_id": "251",
            "format": "251 - audio only (medium)",
            "vcodec": "none",
            "acodec": "opus",
            "abr": 129.5,
            "requested_downloads": [{"filepath": str(media)}],
        }

    return step


async def _replace(settings, job_id, runner):
    from savetube.integrity import Tools
    from savetube.jobs import run

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    # The worker's task runs every queued job through jobs.run.
    await run(job_id, settings, live=False, runner=runner, sessionmaker=sessionmaker, tools=Tools())
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        video = await session.get(Video, "v1")
        watch = await session.get(FormatWatch, "v1")
    await engine.dispose()
    return job, video, watch


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_replace_swaps_the_audio_and_keeps_the_recording_aside(settings, tmp_path):
    from test_audio import make_media

    recording = make_media(tmp_path / "recording.mkv", audio="aac")
    opus = make_media(tmp_path / "opus.webm", audio="libopus", video=False)
    directory = _stored_live(settings, media=recording.read_bytes())
    (directory / "v1.info.json").write_text(json.dumps({"id": "v1", "format_id": "137+140"}))
    (directory / "v1.m4a").write_bytes(b"old audio copy")
    _sql(
        settings,
        "INSERT INTO job (id, kind, state, video_id, format_id, attempts)"
        " VALUES (1, 'replace', 'queued', 'v1', '251', 0),"
        " (2, 'replace', 'queued', 'v1', '251', 0)",
        "UPDATE format_watch SET vod_duration = 1",
    )

    job, video, watch = await _replace(settings, 1, _Steps(_opus_download(opus)))

    assert job.state is JobState.SUCCEEDED, job.log_tail
    assert _ffprobe_streams(directory / "v1.mkv") == [("video", "mpeg4"), ("audio", "opus")]
    aside = settings.archive_root / ".originals" / "v1" / "v1.mkv"
    assert aside.read_bytes() == recording.read_bytes()
    assert watch.original_path == ".originals/v1/v1.mkv"
    # The open watch keeps it until its end.
    assert watch.original_until is None
    assert sorted(p.name for p in directory.iterdir()) == ["v1.info.json", "v1.mkv", "v1.opus"]
    assert video.format_id == "137+251"
    # The kept stream as stored, the downloaded one with its measured size.
    assert video.info["requested_formats"] == [
        WBJ_RECORDING["requested_formats"][0],
        {"format_id": "251", "format": "251 - audio only (medium)", "vcodec": "none",
         "acodec": "opus", "abr": 129.5, "filesize": opus.stat().st_size},
    ]  # fmt: skip
    assert json.loads((directory / "v1.info.json").read_text())["format_id"] == "137+251"
    assert video.size_bytes == sum(p.stat().st_size for p in directory.iterdir())

    # A later replace drops the first replace's file and keeps the recording aside.
    job, _, watch = await _replace(settings, 2, _Steps(_opus_download(opus)))

    assert job.state is JobState.SUCCEEDED, job.log_tail
    assert aside.read_bytes() == recording.read_bytes()
    assert sorted(p.name for p in aside.parent.iterdir()) == ["v1.mkv"]


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_manual_swap_without_a_watch_keeps_the_original_7_days(settings, tmp_path):
    from test_audio import make_media

    recording = make_media(tmp_path / "recording.mkv", audio="aac")
    opus = make_media(tmp_path / "opus.webm", audio="libopus", video=False)
    _stored_live(settings, video_type="videos", media=recording.read_bytes(), watched=False)
    _sql(
        settings,
        "INSERT INTO format_watch (video_id, started_at, vod_duration) VALUES ('v1', NULL, 1)",
        "INSERT INTO job (id, kind, state, video_id, format_id, attempts, manual)"
        " VALUES (1, 'replace', 'queued', 'v1', '251', 0, true)",
    )

    job, _, watch = await _replace(settings, 1, _Steps(_opus_download(opus)))

    assert job.state is JobState.SUCCEEDED, job.log_tail
    aside = settings.archive_root / ".originals" / "v1" / "v1.mkv"
    assert aside.read_bytes() == recording.read_bytes()
    assert watch.original_until == job.finished_at + timedelta(days=7)


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_replace_that_fails_its_checks_leaves_the_stored_file(settings, tmp_path):
    from test_audio import make_media

    recording = make_media(tmp_path / "recording.mkv", audio="aac")
    opus = make_media(tmp_path / "opus.webm", audio="libopus", video=False)
    directory = _stored_live(settings, media=recording.read_bytes())
    _sql(
        settings,
        "INSERT INTO job (id, kind, state, video_id, format_id, attempts)"
        " VALUES (1, 'replace', 'queued', 'v1', '251', 0)",
    )

    async def short(params, report):
        info = await _opus_download(opus)(params, report)
        # The VOD runs 60 s, the downloaded stream 1 s.
        return {**info, "duration": 60}

    job, video, watch = await _replace(settings, 1, _Steps(short))

    assert job.state is JobState.FAILED and "integrity check failed" in job.log_tail
    assert (directory / "v1.mkv").read_bytes() == recording.read_bytes()
    assert watch.original_path is None and video.format_id == "137+140"
    assert not (settings.archive_root / ".originals").exists()
    assert not (settings.archive_root / ".incoming" / "1").exists()
