"""The format watch of stored lives (docs/plan/live.md, Format watch)."""

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL, _api, _events, _sql, _sql_rows, _Steps
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


def _stored_live(settings, *, info=None, video_type="lives", watch_sql="", media=b"recording"):
    """Live v1 stored complete in the tree with its recording, and its watch."""
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
        "INSERT INTO format_watch (video_id, started_at) VALUES ('v1', now())",
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

    async def record(params, report):
        home = Path(params["paths"]["home"])
        (home / "v1.mkv").write_bytes(b"recording")
        media = home / "v1.mkv"
        return {"id": "v1", "title": "t", "channel_id": "UC1", "live_status": "is_live",
                "ext": "mkv", "requested_downloads": [{"filepath": str(media)}]}  # fmt: skip

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await run(1, settings, live=True, runner=_Steps(record), sessionmaker=sessionmaker)
    async with sessionmaker() as session:
        video = await session.get(Video, "v1")
        watch = await session.get(FormatWatch, "v1")
    await engine.dispose()

    assert watch is not None and watch.started_at == video.completed_at


@pytest.mark.anyio
async def test_an_archive_download_that_is_no_live_starts_no_watch(settings):
    from conftest import _finished, _job_rows, _run_steps

    _job_rows(settings)

    _, video = await _run_steps(settings, _Steps(_finished()))

    assert video.state.value == "complete"
    assert _sql_rows(settings, "SELECT video_id FROM format_watch") == []


@pytest.mark.anyio
async def test_a_staged_vod_starts_its_watch_when_it_moves_into_the_tree(settings):
    from savetube.staging import move_staged

    staged = settings.model_copy(update={"staging_root": settings.archive_root.parent / "staging"})
    ready = staged.staging_root / "ready" / "s1"
    ready.mkdir(parents=True)
    (ready / "s1.mkv").write_bytes(b"x")
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, live_status, dir_path,"
        " size_bytes, completed_at) VALUES ('s1', 'UC1', 'lives', 't', 'staged', 'was_live',"
        " 'A [UC1]/lives/s1', 1, now() - interval '2 hours')",
        "UPDATE download_settings SET move_requested_at = now()",
    )
    assert _sql_rows(settings, "SELECT video_id FROM format_watch") == []

    engine = make_engine(settings.sqlalchemy_url)
    await move_staged(make_sessionmaker(engine), staged)
    await engine.dispose()

    rows = _sql_rows(settings, "SELECT video_id, started_at > now() - interval '1 minute'"
                     " FROM format_watch")  # fmt: skip
    assert rows == [("s1", True)]


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

    command.upgrade(config, "head")

    rows = _sql_rows(
        settings,
        "SELECT w.video_id, w.started_at = v.completed_at FROM format_watch w"
        " JOIN video v ON v.id = w.video_id ORDER BY 1",
    )
    assert rows == [("new", True), ("vod", True)]


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


def _watched_with_formats(settings, *, stored_duration=8312.0):
    from savetube.formats import counted

    _stored_live(settings)
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
        ("137", True, "the stored H.264 1.1 Mbps is not below 476 kbps"),
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
        r = api.post("/api/videos/v1/formats/pick", json={"format_id": "243"})
        again = api.post("/api/videos/v1/formats/pick", json={"format_id": "251"})

    assert r.status_code == 200
    assert r.json()["manual"] is True and r.json()["replace"]["format_id"] == "243"
    rows = _sql_rows(settings, "SELECT kind, state, format_id FROM job")
    assert rows == [("replace", "queued", "243")]
    assert again.status_code == 409


def test_api_pick_of_a_held_video_needs_a_confirm(settings):
    _watched_with_formats(settings, stored_duration=8300.0)
    with _api(settings) as api:
        refused = api.post("/api/videos/v1/formats/pick", json={"format_id": "251"})
        confirmed = api.post(
            "/api/videos/v1/formats/pick", json={"format_id": "251", "confirm": True}
        )

    assert refused.status_code == 409 and "12.0 s" in refused.json()["detail"]
    assert confirmed.status_code == 200 and confirmed.json()["held"] is True


def test_api_pick_refuses_a_format_the_vod_does_not_offer(settings):
    _watched_with_formats(settings)

    with _api(settings) as api:
        r = api.post("/api/videos/v1/formats/pick", json={"format_id": "251-drc"})

    assert r.status_code == 404
    assert _sql_rows(settings, "SELECT id FROM job") == []


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
    assert sorted(p.name for p in directory.iterdir()) == ["v1.info.json", "v1.mkv", "v1.opus"]
    assert video.format_id == "137+251"
    assert json.loads((directory / "v1.info.json").read_text())["format_id"] == "137+251"
    assert video.size_bytes == sum(p.stat().st_size for p in directory.iterdir())

    # A later replace drops the first replace's file and keeps the recording aside.
    job, _, watch = await _replace(settings, 2, _Steps(_opus_download(opus)))

    assert job.state is JobState.SUCCEEDED, job.log_tail
    assert aside.read_bytes() == recording.read_bytes()
    assert sorted(p.name for p in aside.parent.iterdir()) == ["v1.mkv"]


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
