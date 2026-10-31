"""External re-encoding (docs/plan/encode.md): queueing, tracking the job
files, checking and swapping a result, and the routes."""

import asyncio
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from conftest import ADMIN_URL, _api, _sql, _sql_rows

from savetube.db import make_engine, make_sessionmaker

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg or ffprobe not on PATH",
)

PROFILE = "vtuber-av1-cq46"
H264 = {
    "format_id": "137",
    "vcodec": "avc1.640028",
    "acodec": "none",
    "width": 64,
    "height": 64,
    "fps": 60,
    "protocol": "https",
    "ext": "mp4",
}
AAC = {
    "format_id": "140",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "protocol": "https",
    "ext": "m4a",
}
VP9 = {**H264, "format_id": "248", "vcodec": "vp09.00.40.08", "ext": "webm"}
# AV1 below the stored height does not count as offered.
AV1_LOW = {**H264, "format_id": "394", "vcodec": "av01.0.00M.08", "width": 32, "height": 32}
AV1_SAME = {**AV1_LOW, "format_id": "399", "width": 64, "height": 64}


def _dir(video_id):
    return f"A [UC1]/videos/2026-10-24 t [{video_id}]"


def _info(video_id, video=H264, formats=(AV1_LOW, H264, AAC)):
    info = {"id": video_id, "ext": "mp4", "format_id": f"{video['format_id']}+140"}
    info["requested_formats"] = [video, AAC]
    if formats is not None:
        info["formats"] = list(formats)
    return info


def _jsonb(value):
    return "CAST('" + json.dumps(value).replace("'", "''") + "' AS jsonb)"


def _stored(settings, video_id="v1", *, media=b"m" * 1000, info=None, state="complete"):
    """Video `video_id` stored as `<video id>.mp4` in its directory, with the
    channel row on first use."""
    info = info or _info(video_id)
    directory = settings.archive_root / _dir(video_id)
    directory.mkdir(parents=True)
    (directory / f"{video_id}.mp4").write_bytes(media)
    if not _sql_rows(settings, "SELECT 1 FROM channel WHERE id = 'UC1'"):
        _sql(settings, "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')")
    _sql(
        settings,
        "INSERT INTO video (id, channel_id, type, title, state, dir_path, completed_at,"
        f" format_id, size_bytes, info) VALUES ('{video_id}', 'UC1', 'videos', 'title {video_id}',"
        f" '{state}', '{_dir(video_id)}', now(), '{info['format_id']}', {len(media)},"
        f" {_jsonb(info)})",
    )
    return directory


def _enable(settings):
    _sql(settings, "UPDATE encode_settings SET enabled = true")


def _enc(settings):
    return settings.archive_root / ".encode"


async def _step(settings, *, now=None, skipped=None):
    from savetube.encode import step
    from savetube.integrity import Tools

    engine = make_engine(settings.sqlalchemy_url)
    try:
        await step(
            make_sessionmaker(engine),
            settings,
            tools=Tools(),
            lock=asyncio.Lock(),
            skipped=set() if skipped is None else skipped,
            now=now,
        )
    finally:
        await engine.dispose()


def _jobs(settings):
    return _sql_rows(
        settings,
        "SELECT id, video_id, state, profile, source_path, source_size, encoder, ffmpeg_version,"
        " result_size, reason, log_tail, finished_at FROM encode_job ORDER BY id",
    )


def _queue_file(settings):
    [path] = (_enc(settings) / "queue").iterdir()
    return path, json.loads(path.read_text())


# Queueing --------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_complete_h264_video_gets_one_queue_file(settings):
    _stored(settings, media=b"m" * 5000)
    _enable(settings)

    await _step(settings, now=datetime.now(UTC))

    path, job = _queue_file(settings)
    [row] = _jobs(settings)
    assert path.name == f"{row.id}.json"
    assert {k: job[k] for k in ("job_id", "video_id", "source", "profile")} == {
        "job_id": row.id,
        "video_id": "v1",
        "source": f"{_dir('v1')}/v1.mp4",
        "profile": PROFILE,
    }
    assert datetime.fromisoformat(job["created_at"])
    assert (row.video_id, row.state, row.profile, row.source_path, row.source_size) == (
        "v1",
        "queued",
        PROFILE,
        f"{_dir('v1')}/v1.mp4",
        5000,
    )


@pytest.mark.anyio
async def test_nothing_is_queued_while_disabled(settings):
    _stored(settings)

    await _step(settings, now=datetime.now(UTC))

    assert _jobs(settings) == []
    assert not list(_enc(settings).glob("queue/*"))


@pytest.mark.anyio
async def test_the_queue_holds_the_3_largest_videos(settings):
    for video_id, size in (("s10", 10), ("s40", 40), ("s20", 20), ("s30", 30)):
        _stored(settings, video_id, media=b"m" * size)
    _enable(settings)

    await _step(settings, now=datetime.now(UTC))
    await _step(settings, now=datetime.now(UTC))

    assert sorted((r.video_id, r.state) for r in _jobs(settings)) == [
        ("s20", "queued"),
        ("s30", "queued"),
        ("s40", "queued"),
    ]
    assert len(list((_enc(settings) / "queue").iterdir())) == 3


def _encode_row(state):
    return (
        "INSERT INTO encode_job (video_id, state, profile, source_path, source_size)"
        f" VALUES ('v1', '{state}', '{PROFILE}', '{_dir('v1')}/v1.mp4', 1000)"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("info", "extra_sql", "on_disk", "queued", "remembered"),
    [
        pytest.param(None, [], True, True, False, id="candidate"),
        pytest.param(_info("v1", video=VP9), [], True, False, False, id="not-h264"),
        pytest.param(None, ["UPDATE video SET state = 'downloading'"], True, False, False,
                     id="not-complete"),
        pytest.param(None, ["INSERT INTO job (kind, state, video_id, attempts)"
                            " VALUES ('replace', 'queued', 'v1', 0)"], True, False, False,
                     id="replace-queued"),
        pytest.param(None, ["INSERT INTO job (kind, state, video_id, attempts)"
                            " VALUES ('record', 'running', 'v1', 1)"], True, False, False,
                     id="record-running"),
        pytest.param(None, ["INSERT INTO format_watch (video_id, started_at, original_path)"
                            " VALUES ('v1', NULL, '.originals/v1/v1.mp4')"], True, False, False,
                     id="original-kept"),
        pytest.param(None, ["INSERT INTO format_watch (video_id, started_at)"
                            " VALUES ('v1', now())"], True, False, False, id="open-watch"),
        pytest.param(None, ["INSERT INTO format_watch (video_id, started_at, ended_at)"
                            " VALUES ('v1', now() - interval '8 days', now())"], True, True, False,
                     id="ended-watch"),
        *[
            pytest.param(None, [_encode_row(state)], True, False, False, id=f"encode-{state}")
            for state in ("queued", "claimed", "failed", "succeeded")
        ],
        *[
            pytest.param(None, [_encode_row(state)], True, True, False, id=f"encode-{state}")
            for state in ("cancelled", "lost")
        ],
        pytest.param(_info("v1", formats=(AV1_SAME, H264, AAC)), [], True, False, True,
                     id="av1-in-info"),
        pytest.param(_info("v1", formats=None), [], True, False, True, id="no-format-list"),
        pytest.param(None, ["INSERT INTO format_watch (video_id, started_at, formats)"
                            f" VALUES ('v1', NULL, {_jsonb([AV1_SAME, H264, AAC])})"],
                     True, False, True, id="av1-in-probe"),
        # The probed list replaces the info dict's.
        pytest.param(_info("v1", formats=(AV1_SAME, H264, AAC)),
                     ["INSERT INTO format_watch (video_id, started_at, formats)"
                      f" VALUES ('v1', NULL, {_jsonb([H264, AAC])})"], True, True, False,
                     id="probe-without-av1"),
        pytest.param(None, [], False, False, False, id="file-missing"),
    ],
)  # fmt: skip
async def test_queueing_rules(settings, info, extra_sql, on_disk, queued, remembered):
    directory = _stored(settings, info=info)
    if extra_sql:
        _sql(settings, *extra_sql)
    if not on_disk:
        (directory / "v1.mp4").unlink()
    _enable(settings)
    before = len(_jobs(settings))
    skipped = set()

    await _step(settings, now=datetime.now(UTC), skipped=skipped)

    new = [r for r in _jobs(settings)[before:] if r.state == "queued"]
    assert len(new) == (1 if queued else 0)
    assert len(list(_enc(settings).glob("queue/*.json"))) == (1 if queued else 0)
    if remembered:
        assert "v1" in skipped


# Tracking the job files ------------------------------------------------------


async def _queued(settings, now):
    _stored(settings)
    _enable(settings)
    await _step(settings, now=now)
    return _queue_file(settings)


def _state(settings, job_id):
    return next(r for r in _jobs(settings) if r.id == job_id)


@pytest.mark.anyio
@pytest.mark.parametrize("claimed", ["claimed", "work"])
async def test_a_claimed_job_file_marks_the_job_claimed(settings, claimed):
    t = datetime.now(UTC)
    path, job = await _queued(settings, t)
    (_enc(settings) / claimed).mkdir(exist_ok=True)
    if claimed == "claimed":
        path.rename(_enc(settings) / "claimed" / path.name)
    else:
        path.unlink()
        (_enc(settings) / "work" / f"{job['job_id']}.mkv").write_bytes(b"partial")

    await _step(settings, now=t + timedelta(minutes=1))

    assert _state(settings, job["job_id"]).state == "claimed"
    assert [r.id for r in _jobs(settings)] == [job["job_id"]]


@pytest.mark.anyio
async def test_a_job_file_gone_10_minutes_after_queueing_is_lost(settings):
    t = datetime.now(UTC)
    path, job = await _queued(settings, t)
    path.unlink()

    await _step(settings, now=t + timedelta(minutes=5))
    assert _state(settings, job["job_id"]).state == "queued"

    await _step(settings, now=t + timedelta(minutes=11))
    assert _state(settings, job["job_id"]).state == "lost"


@pytest.mark.anyio
async def test_a_claimed_file_counts_10_minutes_from_when_it_was_last_seen(settings):
    t = datetime.now(UTC)
    path, job = await _queued(settings, t)
    claimed = _enc(settings) / "claimed" / path.name
    claimed.parent.mkdir(exist_ok=True)
    path.rename(claimed)
    await _step(settings, now=t + timedelta(minutes=9))
    claimed.unlink()

    await _step(settings, now=t + timedelta(minutes=15))
    assert _state(settings, job["job_id"]).state == "claimed"

    await _step(settings, now=t + timedelta(minutes=20))
    assert _state(settings, job["job_id"]).state == "lost"


async def _failed_by_encoder(settings, t):
    path, job = await _queued(settings, t)
    failed = _enc(settings) / "failed"
    failed.mkdir(exist_ok=True)
    path.unlink()
    (failed / path.name).write_text(
        json.dumps(
            {
                **job,
                "reason": "nvenc init failed",
                "log_tail": "error line",
                "failed_at": t.isoformat(),
            }
        )
    )
    await _step(settings, now=t + timedelta(minutes=1))
    return job


@pytest.mark.anyio
async def test_a_failed_job_file_fails_the_job_and_the_video_waits(settings):
    t = datetime.now(UTC)

    job = await _failed_by_encoder(settings, t)

    [row] = _jobs(settings)
    assert (row.id, row.state, row.reason, row.log_tail) == (
        job["job_id"],
        "failed",
        "nvenc init failed",
        "error line",
    )
    assert not list((_enc(settings) / "queue").iterdir())


@pytest.mark.anyio
async def test_a_requeued_failure_drops_its_files_and_queues_the_video_again(settings):
    t = datetime.now(UTC)
    job = await _failed_by_encoder(settings, t)
    with _api(settings) as api:
        r = api.post(f"/api/encode/jobs/{job['job_id']}/requeue")
    assert r.is_success

    await _step(settings, now=t + timedelta(minutes=2))

    assert not list((_enc(settings) / "failed").iterdir())
    assert [(r.id == job["job_id"], r.state) for r in _jobs(settings)] == [
        (True, "requeued"),
        (False, "queued"),
    ]


# Checking and swapping a result ----------------------------------------------


def _source_media(path):
    """2 s of H.264 at 60 fps with AAC, lossless so it is larger than the result."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=d=2:s=64x64:r=60", "-f", "lavfi", "-i", "sine=d=2",
         "-c:v", "libx264", "-qp", "0", "-c:a", "aac", "-ar", "48000", str(path)],
        check=True,
    )  # fmt: skip
    return path


def _encode(source, out, *, fps=30, pix_fmt="yuv420p10le"):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
         "-map", "0:v", "-map", "0:a", "-c:v", "libsvtav1", "-crf", "50",
         "-pix_fmt", pix_fmt, "-r", str(fps), "-c:a", "copy", str(out)],
        check=True,
    )  # fmt: skip
    return out


def _done(settings, job, result, t, **override):
    """The encoder's done/ pair for `job`, with `override` in its job file."""
    done = _enc(settings) / "done"
    done.mkdir(exist_ok=True)
    mkv = done / f"{job['job_id']}.mkv"
    shutil.copyfile(result, mkv)
    data = {
        **job,
        "profile": PROFILE,
        "encoder": "av1_nvenc",
        "ffmpeg_version": "7.1.1",
        "sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
        "size": result.stat().st_size,
        "duration": 2.0,
        "finished_at": t.isoformat(),
        **override,
    }
    (done / f"{job['job_id']}.json").write_text(json.dumps(data))
    return data


async def _with_result(settings, tmp_path, t, **encode):
    source = _source_media(tmp_path / "source.mp4")
    directory = _stored(settings, media=source.read_bytes())
    _enable(settings)
    await _step(settings, now=t)
    path, job = _queue_file(settings)
    path.unlink()
    result = _encode(source, tmp_path / "result.mkv", **encode)
    assert result.stat().st_size < source.stat().st_size
    return source, directory, job, result


def _video(settings):
    [row] = _sql_rows(settings, "SELECT info, format_id, size_bytes FROM video WHERE id = 'v1'")
    return row


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_valid_result_replaces_the_stored_file(settings, tmp_path):
    t = datetime.now(UTC)
    source, directory, job, result = await _with_result(settings, tmp_path, t)
    (directory / "v1.info.json").write_text(json.dumps(_info("v1")))
    _done(settings, job, result, t)
    inode = (directory / "v1.mp4").stat().st_ino
    later = t + timedelta(minutes=1)

    await _step(settings, now=later)

    row = _state(settings, job["job_id"])
    assert (row.state, row.encoder, row.ffmpeg_version, row.result_size) == (
        "succeeded",
        "av1_nvenc",
        "7.1.1",
        result.stat().st_size,
    ), row.reason
    aside = settings.archive_root / ".originals" / "v1" / "v1.mp4"
    assert aside.stat().st_ino == inode and aside.read_bytes() == source.read_bytes()
    assert not (directory / "v1.mp4").exists()
    assert (directory / "v1.mkv").read_bytes() == result.read_bytes()
    assert not list((_enc(settings) / "done").iterdir())
    [watch] = _sql_rows(
        settings, "SELECT original_path, original_until FROM format_watch WHERE video_id = 'v1'"
    )
    assert (watch.original_path, watch.original_until) == (
        ".originals/v1/v1.mp4",
        later + timedelta(days=7),
    )
    video = _video(settings)
    new_video, audio = video.info["requested_formats"]
    assert (new_video["vcodec"], new_video["format_id"], new_video["format_note"]) == (
        "av01",
        PROFILE,
        "re-encoded",
    )
    assert audio == AAC
    assert video.format_id == f"{PROFILE}+140"
    assert video.size_bytes == sum(p.stat().st_size for p in directory.iterdir())
    written = json.loads((directory / "v1.info.json").read_text())
    assert written["requested_formats"][0]["vcodec"] == "av01"


def _assert_failed(settings, job, data):
    failed = _enc(settings) / "failed"
    assert sorted(p.name for p in failed.iterdir()) == [
        f"{job['job_id']}.json",
        f"{job['job_id']}.mkv",
    ]
    moved = json.loads((failed / f"{job['job_id']}.json").read_text())
    assert moved["reason"] and moved["failed_at"]
    # The failed job file carries {reason, log_tail, failed_at} (encode.md, Job files).
    assert {k: v for k, v in moved.items() if k not in ("reason", "log_tail", "failed_at")} == data
    assert not list((_enc(settings) / "done").iterdir())
    row = _state(settings, job["job_id"])
    assert row.state == "failed" and row.reason == moved["reason"]
    assert not (settings.archive_root / ".originals").exists()
    assert _video(settings).format_id == "137+140"


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_result_with_a_wrong_sha256_moves_to_failed(settings, tmp_path):
    t = datetime.now(UTC)
    source, directory, job, result = await _with_result(settings, tmp_path, t)
    data = _done(settings, job, result, t, sha256="0" * 64)

    await _step(settings, now=t + timedelta(minutes=1))

    _assert_failed(settings, job, data)
    assert sorted(p.name for p in directory.iterdir()) == ["v1.mp4"]
    assert (directory / "v1.mp4").read_bytes() == source.read_bytes()


@needs_ffmpeg
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("encode", "override"),
    [
        pytest.param({}, {"size": None}, id="wrong-size"),
        pytest.param({}, {"profile": "other-profile"}, id="wrong-profile"),
        pytest.param({"fps": 60}, {}, id="fps-above-30"),
        pytest.param({"pix_fmt": "yuv420p"}, {}, id="8-bit"),
    ],
)
async def test_a_result_failing_a_check_moves_to_failed(settings, tmp_path, encode, override):
    t = datetime.now(UTC)
    source, directory, job, result = await _with_result(settings, tmp_path, t, **encode)
    if override.get("size", 0) is None:
        override = {"size": result.stat().st_size + 1}
    data = _done(settings, job, result, t, **override)

    await _step(settings, now=t + timedelta(minutes=1))

    _assert_failed(settings, job, data)
    assert (directory / "v1.mp4").read_bytes() == source.read_bytes()


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_stored_file_changed_since_queueing_fails_the_result(settings, tmp_path):
    t = datetime.now(UTC)
    source, directory, job, result = await _with_result(settings, tmp_path, t)
    with (directory / "v1.mp4").open("ab") as f:
        f.write(b"tail")
    data = _done(settings, job, result, t)

    await _step(settings, now=t + timedelta(minutes=1))

    _assert_failed(settings, job, data)
    assert (directory / "v1.mp4").read_bytes() == source.read_bytes() + b"tail"


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_result_waits_while_a_replace_is_queued(settings, tmp_path):
    t = datetime.now(UTC)
    source, directory, job, result = await _with_result(settings, tmp_path, t)
    _done(settings, job, result, t)
    _sql(
        settings,
        "INSERT INTO job (kind, state, video_id, format_id, attempts)"
        " VALUES ('replace', 'queued', 'v1', '251', 0)",
    )

    await _step(settings, now=t + timedelta(minutes=1))

    assert sorted(p.name for p in (_enc(settings) / "done").iterdir()) == [
        f"{job['job_id']}.json",
        f"{job['job_id']}.mkv",
    ]
    assert _state(settings, job["job_id"]).state not in ("failed", "succeeded")
    assert (directory / "v1.mp4").read_bytes() == source.read_bytes()


@pytest.mark.anyio
async def test_a_done_file_without_a_job_moves_to_failed(settings):
    done = _enc(settings) / "done"
    done.mkdir(parents=True)
    (done / "999.mkv").write_bytes(b"result")
    (done / "999.json").write_text(
        json.dumps({"job_id": 999, "video_id": "zz", "profile": PROFILE})
    )

    await _step(settings, now=datetime.now(UTC))

    assert not (done / "999.json").exists()
    assert json.loads((_enc(settings) / "failed" / "999.json").read_text())["reason"]


# Routes ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("job_id", "setup", "status", "state"),
    [
        ("own", None, 204, "cancelled"),
        (999, None, 404, None),
        ("own", "claim", 409, "queued"),
        ("own", "fail", 409, "failed"),
    ],
)
async def test_scheduler_cancel(settings, job_id, setup, status, state):
    from savetube.integrity import Tools
    from savetube.scheduler_api import Requests, create_app

    path, job = await _queued(settings, datetime.now(UTC))
    if setup == "claim":
        (_enc(settings) / "claimed").mkdir(exist_ok=True)
        path.rename(_enc(settings) / "claimed" / path.name)
    if setup == "fail":
        _sql(settings, f"UPDATE encode_job SET state = 'failed' WHERE id = {job['job_id']}")
    target = job["job_id"] if job_id == "own" else job_id
    engine = make_engine(settings.sqlalchemy_url)
    app = create_app(
        settings,
        make_sessionmaker(engine),
        probes=asyncio.Lock(),
        probe=lambda v, c: pytest.fail("no probe expected"),
        tools=Tools(),
        requests=Requests(),
        encodes=asyncio.Lock(),
    )
    try:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://scheduler"
        ) as client:
            r = await client.post(f"/encode/{target}/cancel")
    finally:
        await engine.dispose()

    assert r.status_code == status
    if state:
        assert _state(settings, job["job_id"]).state == state
    if status == 204:
        assert not path.exists()
    if setup is None and status != 204:
        assert path.exists()


class _Scheduler:
    """The scheduler's API behind the web's outbound httpx2 calls
    (test_db_scheduler_requests._Scheduler)."""

    def __init__(self, settings):
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from savetube.db import PSYCOPG_CONNECT_ARGS
        from savetube.integrity import Tools
        from savetube.scheduler_api import Requests, create_app

        engine = create_async_engine(
            settings.sqlalchemy_url, connect_args=PSYCOPG_CONNECT_ARGS, poolclass=NullPool
        )
        self.app = create_app(
            settings,
            make_sessionmaker(engine),
            probes=asyncio.Lock(),
            probe=lambda v, c: pytest.fail("no probe expected"),
            tools=Tools(),
            requests=Requests(),
            encodes=asyncio.Lock(),
        )
        self.urls = []

    async def handle(self, request):
        self.urls.append(str(request.url))
        return await httpx2.ASGITransport(app=self.app).handle_async_request(request)


def test_api_cancel_asks_the_scheduler(settings, monkeypatch):
    asyncio.run(_queued(settings, datetime.now(UTC)))
    path, job = _queue_file(settings)
    scheduler = _Scheduler(settings)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)

    with _api(settings) as api:
        r = api.post(f"/api/encode/jobs/{job['job_id']}/cancel")
        jobs = api.get("/api/encode").json()["jobs"]

    assert r.is_success
    assert scheduler.urls == [f"{settings.scheduler_url}/encode/{job['job_id']}/cancel"]
    assert not path.exists()
    assert [j["state"] for j in jobs] == ["cancelled"]


def test_api_settings_start_off_and_save(settings):
    with _api(settings) as api:
        first = api.get("/api/encode").json()
        put = api.put("/api/encode", json={"enabled": True, "profile": PROFILE})
        unknown = api.put("/api/encode", json={"enabled": True, "profile": "av1-cq99"})
        after = api.get("/api/encode").json()

    assert first == {"enabled": False, "profile": PROFILE, "profiles": [PROFILE], "jobs": []}
    assert put.is_success and unknown.status_code == 422
    assert (after["enabled"], after["profile"]) == (True, PROFILE)


def test_api_lists_jobs_and_requeues_only_failures(settings):
    _stored(settings)
    _sql(
        settings,
        "INSERT INTO encode_job (id, video_id, state, profile, source_path, source_size, reason,"
        f" finished_at) VALUES (7, 'v1', 'failed', '{PROFILE}', '{_dir('v1')}/v1.mp4', 1000,"
        " 'sha256 mismatch', now()),"
        f" (8, 'v1', 'queued', '{PROFILE}', '{_dir('v1')}/v1.mp4', 1000, NULL, NULL)",
    )

    with _api(settings) as api:
        listed = {j["id"]: j for j in api.get("/api/encode").json()["jobs"]}
        queued = api.post("/api/encode/jobs/8/requeue")
        unknown = api.post("/api/encode/jobs/99/requeue")
        requeued = api.post("/api/encode/jobs/7/requeue")
        again = api.post("/api/encode/jobs/7/requeue")
        after = {j["id"]: j["state"] for j in api.get("/api/encode").json()["jobs"]}

    failed = listed[7]
    assert {k: failed[k] for k in ("video_id", "title", "state", "profile", "source_size",
                                   "result_size", "reason")} == {
        "video_id": "v1", "title": "title v1", "state": "failed", "profile": PROFILE,
        "source_size": 1000, "result_size": None, "reason": "sha256 mismatch",
    }  # fmt: skip
    assert failed["created_at"] and failed["finished_at"]
    assert (queued.status_code, unknown.status_code, again.status_code) == (409, 404, 409)
    assert requeued.is_success
    assert after == {7: "requeued", 8: "queued"}
