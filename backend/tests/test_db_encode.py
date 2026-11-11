"""External re-encoding (docs/plan/encode.md): queueing, tracking the job
files, checking and swapping a result, and the routes."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL, _api, _sql, _sql_rows

from savetube.db import make_engine, make_sessionmaker

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg or ffprobe not on PATH",
)

# The name _enable's options derive (docs/plan/encode.md, Profiles).
PROFILE = "av1-uhq-cq46-30fps"
# Named profiles a job queued before the options carries.
LEGACY = "vtuber-av1-uhq-cq46"
LEGACY_SRCFPS = "vtuber-av1-uhq-cq46-srcfps"
# The uhq profile's old name, accepted only for jobs queued under it.
RETIRED = "vtuber-av1-cq46"
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


@pytest.fixture(autouse=True)
def _tiny_sources(monkeypatch):
    """The fixtures' stored files are a few KiB."""
    monkeypatch.setattr("savetube.encode.MIN_SOURCE_BYTES", 0)


def _enable(settings, **fields):
    """Re-encoding on, with uhq at a 30 fps cap and no motion measurement
    unless `fields` say otherwise."""
    fields = {"tune": "uhq", "fps_cap": 30, "motion_threshold": None, **fields}
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.begin() as c:
        c.execute(sa.text(f"UPDATE encode_settings SET enabled = true, {sets}"), fields)
    engine.dispose()


def _enc(settings):
    return settings.archive_root / ".encode"


async def _step(settings, *, now=None, skipped=None, tools=None):
    from savetube.encode import step
    from savetube.integrity import Tools

    engine = make_engine(settings.sqlalchemy_url)
    try:
        await step(
            make_sessionmaker(engine),
            settings,
            tools=tools or Tools(),
            lock=asyncio.Lock(),
            skipped=set() if skipped is None else skipped,
            now=now,
        )
    finally:
        await engine.dispose()


def _jobs(settings):
    return _sql_rows(
        settings,
        "SELECT id, video_id, state, profile, options, source_path, source_size, encoder,"
        " ffmpeg_version, result_size, reason, log_tail, finished_at FROM encode_job ORDER BY id",
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
    assert {k: v for k, v in job.items() if k != "created_at"} == {
        "job_id": row.id,
        "video_id": "v1",
        "source": f"{_dir('v1')}/v1.mp4",
        "profile": "av1-uhq-cq46-30fps",
        "options": {"tune": "uhq", "cq": 46, "fps_cap": 30},
    }
    assert datetime.fromisoformat(job["created_at"])
    assert (row.video_id, row.state, row.profile, row.source_path, row.source_size) == (
        "v1",
        "queued",
        "av1-uhq-cq46-30fps",
        f"{_dir('v1')}/v1.mp4",
        5000,
    )
    assert row.options == {"tune": "uhq", "cq": 46, "fps_cap": 30}


@pytest.mark.anyio
async def test_nothing_is_queued_while_disabled(settings):
    _stored(settings)

    await _step(settings, now=datetime.now(UTC))

    assert _jobs(settings) == []
    assert not list(_enc(settings).glob("queue/*"))


# A still title's options under the migrated defaults with hq, and every
# other title's, which measure the motion (docs/plan/encode.md, Profiles).
STILL = {"tune": "hq", "cq": 46, "fps_cap": 30}
MEASURED = {**STILL, "motion_threshold": 1.0}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fields", "title", "options", "profile"),
    [
        pytest.param({}, "【歌枠】夜のうた", STILL, "av1-hq-cq46-30fps", id="still"),
        pytest.param({}, "asmr 耳かき", STILL, "av1-hq-cq46-30fps", id="still-any-case"),
        pytest.param({}, "【3D LIVE】歌枠スペシャル", MEASURED, "av1-hq-cq46-30fps-motion1",
                     id="3d-live"),
        pytest.param({}, "【Minecraft】建築", MEASURED, "av1-hq-cq46-30fps-motion1", id="plain"),
        pytest.param({"tune": "uhq", "cq": 40, "fps_cap": 29.97}, "【Minecraft】建築",
                     {"tune": "uhq", "cq": 40, "fps_cap": 29.97, "motion_threshold": 1.0},
                     "av1-uhq-cq40-29.97fps-motion1", id="plain-capped"),
        pytest.param({"motion_threshold": 1.5}, "【Minecraft】建築",
                     {**STILL, "motion_threshold": 1.5}, "av1-hq-cq46-30fps-motion1.5",
                     id="plain-own-threshold"),
        # No threshold applies the cap, and no cap keeps the source's rate:
        # neither measures.
        pytest.param({"motion_threshold": None}, "【Minecraft】建築", STILL, "av1-hq-cq46-30fps",
                     id="plain-unmeasured"),
        pytest.param({"fps_cap": None}, "【Minecraft】建築", {**STILL, "fps_cap": None},
                     "av1-hq-cq46-srcfps", id="plain-uncapped"),
        pytest.param({"still_fps_cap": None}, "【雑談】free talk", {**STILL, "fps_cap": None},
                     "av1-hq-cq46-srcfps", id="still-uncapped"),
        pytest.param({"still_fps_cap": 24}, "【雑談】free talk", {**STILL, "fps_cap": 24},
                     "av1-hq-cq46-24fps", id="still-own-cap"),
        pytest.param({"still_words": ["Minecraft"]}, "【Minecraft】建築", STILL,
                     "av1-hq-cq46-30fps", id="own-still-word"),
        pytest.param({"still_words": ["Minecraft"]}, "【歌枠】夜のうた", MEASURED,
                     "av1-hq-cq46-30fps-motion1", id="default-word-removed"),
        pytest.param({"still_exclude_words": ["夜"]}, "【歌枠】夜のうた", MEASURED,
                     "av1-hq-cq46-30fps-motion1", id="own-exclude-word"),
        pytest.param({"still_exclude_words": []}, "【3D LIVE】歌枠スペシャル", STILL,
                     "av1-hq-cq46-30fps", id="no-exclude-words"),
        # No still words: no title counts as still.
        pytest.param({"still_words": []}, "【歌枠】夜のうた", MEASURED,
                     "av1-hq-cq46-30fps-motion1", id="no-still-words"),
    ],
)  # fmt: skip
async def test_a_job_takes_the_options_its_title_resolves(
    settings, fields, title, options, profile
):
    _stored(settings)
    _sql(settings, f"UPDATE video SET title = '{title}' WHERE id = 'v1'")
    # The migrated defaults with hq, then the case's changes.
    _enable(settings, **{"tune": "hq", "fps_cap": 30, "motion_threshold": 1.0, **fields})

    await _step(settings, now=datetime.now(UTC))

    _, job = _queue_file(settings)
    [row] = _jobs(settings)
    assert (job["options"], job["profile"]) == (options, profile)
    assert (row.options, row.profile) == (options, profile)


@pytest.mark.anyio
@pytest.mark.parametrize(("size", "queued"), [(10 * 2**20 - 1, False), (10 * 2**20, True)])
async def test_a_source_under_10_mib_is_not_queued(settings, monkeypatch, size, queued):
    monkeypatch.undo()  # the real minimum, not _tiny_sources's
    directory = _stored(settings)
    os.truncate(directory / "v1.mp4", size)
    _enable(settings)

    await _step(settings, now=datetime.now(UTC))

    assert len(_jobs(settings)) == int(queued)


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
        pytest.param(_info("v1", video=VP9), [], True, True, False, id="vp9"),
        pytest.param(_info("v1", video=AV1_SAME), [], True, False, False, id="already-av1"),
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
            for state in ("cancelled", "lost", "superseded")
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


def _encode(source, out, *, fps=30, pix_fmt="yuv420p10le", codec="libsvtav1", audio=1, extra=()):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
         "-map", "0:v", *["-map", "0:a"] * audio, "-c:v", codec, "-crf", "50",
         "-pix_fmt", pix_fmt, "-r", str(fps), "-c:a", "copy", *extra, str(out)],
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


async def _with_result(settings, tmp_path, t, options=None, **encode):
    source = _source_media(tmp_path / "source.mp4")
    directory = _stored(settings, media=source.read_bytes())
    _enable(settings, **(options or {}))
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


@needs_ffmpeg
@pytest.mark.anyio
async def test_a_result_queued_under_a_retired_profile_still_replaces_the_stored_file(
    settings, tmp_path
):
    t = datetime.now(UTC)
    source, directory, job, result = await _with_result(settings, tmp_path, t)
    _sql(
        settings,
        f"UPDATE encode_job SET profile = '{RETIRED}', options = NULL WHERE id = {job['job_id']}",
    )
    job.pop("options")
    _done(settings, job, result, t, profile=RETIRED)

    await _step(settings, now=t + timedelta(minutes=1))

    row = _state(settings, job["job_id"])
    assert row.state == "succeeded", row.reason
    assert (directory / "v1.mkv").read_bytes() == result.read_bytes()
    assert _video(settings).format_id == f"{RETIRED}+140"


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
@pytest.mark.parametrize(
    ("fps_cap", "fps", "state"),
    [
        pytest.param(30, 30, "succeeded", id="cap-30-at-30"),
        pytest.param(30, 60, "failed", id="cap-30-at-60"),
        pytest.param(None, 60, "succeeded", id="source-rate-at-60"),
        pytest.param(None, 30, "failed", id="source-rate-at-30"),
        # A cap above the source's rate keeps the source's.
        pytest.param(120, 60, "succeeded", id="cap-120-at-60"),
    ],
)
async def test_a_result_runs_at_the_options_frame_rate(settings, tmp_path, fps_cap, fps, state):
    t = datetime.now(UTC)
    _, _, job, result = await _with_result(
        settings, tmp_path, t, options={"fps_cap": fps_cap}, fps=fps
    )
    _done(settings, job, result, t, profile=job["profile"])

    await _step(settings, now=t + timedelta(minutes=1))

    row = _state(settings, job["job_id"])
    assert row.state == state, row.reason


# A measured job's frame rate follows its motion (docs/plan/encode.md,
# Checking and swapping a result). The source runs at 60 fps.
@needs_ffmpeg
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("motion", "fps", "state"),
    [
        pytest.param(2.5, 60, "succeeded", id="above-keeps-60"),
        pytest.param(1.0, 60, "succeeded", id="at-threshold-keeps-60"),
        pytest.param(0.999, 30, "succeeded", id="below-caps-30"),
        pytest.param(0.999, 60, "failed", id="below-at-60"),
        pytest.param(1.0, 30, "failed", id="at-threshold-at-30"),
        pytest.param(None, 30, "failed", id="motion-missing"),
        pytest.param("2.5", 60, "failed", id="motion-not-a-number"),
    ],
)
async def test_a_measured_result_runs_at_the_rate_its_motion_picks(
    settings, tmp_path, motion, fps, state
):
    t = datetime.now(UTC)
    _, _, job, result = await _with_result(
        settings, tmp_path, t, options={"fps_cap": 30, "motion_threshold": 1.0}, fps=fps
    )
    assert job["options"]["motion_threshold"] == 1.0
    measured = {} if motion is None else {"motion": motion, "fps": float(fps)}
    _done(settings, job, result, t, profile=job["profile"], **measured)

    await _step(settings, now=t + timedelta(minutes=1))

    row = _state(settings, job["job_id"])
    assert row.state == state, row.reason


# A job queued before the options names a profile, and its check reads the
# profile's frame rate.
@needs_ffmpeg
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("profile", "fps", "state"),
    [
        pytest.param(LEGACY_SRCFPS, 60, "succeeded", id="srcfps-at-60"),
        pytest.param(LEGACY_SRCFPS, 30, "failed", id="srcfps-at-30"),
        pytest.param(LEGACY, 30, "succeeded", id="30-fps-at-30"),
        pytest.param(LEGACY, 60, "failed", id="30-fps-at-60"),
    ],
)
async def test_a_job_without_options_checks_its_named_profile(
    settings, tmp_path, profile, fps, state
):
    t = datetime.now(UTC)
    # Options that pass every rate, so only the named profile decides.
    _, _, job, result = await _with_result(
        settings, tmp_path, t, options={"fps_cap": None}, fps=fps
    )
    _sql(
        settings,
        f"UPDATE encode_job SET profile = '{profile}', options = NULL WHERE id = {job['job_id']}",
    )
    job.pop("options")
    _done(settings, job, result, t, profile=profile)

    await _step(settings, now=t + timedelta(minutes=1))

    row = _state(settings, job["job_id"])
    assert row.state == state, row.reason


class _PacketErrors:
    """ffprobe as it is, ffmpeg's packet pass reporting a broken packet."""

    def __init__(self):
        from savetube.integrity import Tools

        self.real = Tools()

    def probe(self, path):
        return self.real.probe(path)

    def packets(self, path):
        return "[matroska @ 0x1] Invalid packet at offset 4096"


def _larger_than_source(source, out, tmp_path):
    # An attachment pads a valid encode past the source's size.
    pad = tmp_path / "pad.bin"
    pad.write_bytes(b"\0" * source.stat().st_size)
    attach = ["-attach", str(pad), "-metadata:s:t", "mimetype=application/octet-stream"]
    return _encode(source, out, extra=attach)


# Each row breaks one check of encode.md "Checking and swapping a result", and the
# reason must name it.
@needs_ffmpeg
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("make", "after", "tools", "named"),
    [
        pytest.param(_larger_than_source, None, None, "smaller", id="not-smaller"),
        pytest.param(
            lambda s, o, t: _encode(s, o, audio=2), None, None, "audio", id="audio-streams"
        ),
        pytest.param(
            lambda s, o, t: _encode(s, o, codec="libx265"), None, None, "codec", id="video-codec"
        ),
        pytest.param(
            lambda s, o, t: _encode(s, o, extra=["-s", "32x32"]),
            None,
            None,
            "resolution",
            id="resolution",
        ),
        pytest.param(
            lambda s, o, t: _encode(s, o, extra=["-t", "0.5"]),
            None,
            None,
            "duration",
            id="duration",
        ),
        pytest.param(None, None, _PacketErrors, "packet", id="packet-errors"),
        pytest.param(
            None,
            lambda json_path, mkv: json_path.write_text("{"),
            None,
            "unreadable",
            id="unreadable-job-file",
        ),
        pytest.param(
            None, lambda json_path, mkv: mkv.unlink(), None, "missing", id="missing-result"
        ),
    ],
)
async def test_a_result_failing_a_probe_or_file_check_moves_to_failed(
    settings, tmp_path, make, after, tools, named
):
    t = datetime.now(UTC)
    source, directory, job, result = await _with_result(settings, tmp_path, t)
    if make is not None:
        result = make(source, tmp_path / "bad.mkv", tmp_path)
    _done(settings, job, result, t)
    done = _enc(settings) / "done"
    if after is not None:
        after(done / f"{job['job_id']}.json", done / f"{job['job_id']}.mkv")

    await _step(settings, now=t + timedelta(minutes=1), tools=tools and tools())

    row = _state(settings, job["job_id"])
    assert row.state == "failed", row.reason
    assert named in row.reason.lower()
    assert (directory / "v1.mp4").read_bytes() == source.read_bytes()
    assert not (settings.archive_root / ".originals").exists()
    assert _video(settings).format_id == "137+140"


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
        moves=asyncio.Lock(),
        listings=asyncio.Semaphore(2),
        lister=lambda url, cookiefile, limit: pytest.fail("no listing expected"),
        prober=lambda video_id, cookiefile: pytest.fail("no membership check expected"),
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
            moves=asyncio.Lock(),
            listings=asyncio.Semaphore(2),
            lister=lambda url, cookiefile, limit: pytest.fail("no listing expected"),
            prober=lambda video_id, cookiefile: pytest.fail("no membership check expected"),
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
        jobs = api.get("/api/encode").json()["finished"]["items"]

    assert r.is_success
    assert scheduler.urls == [f"{settings.scheduler_url}/encode/{job['job_id']}/cancel"]
    assert not path.exists()
    assert [j["state"] for j in jobs] == ["cancelled"]


SETTINGS = {
    "enabled": True,
    "tune": "hq",
    "cq": 40,
    "fps_cap": 29.97,
    "still_fps_cap": None,
    "motion_threshold": 1.5,
    "still_words": ["歌枠"],
    "still_exclude_words": [],
}


def test_api_settings_start_off_and_save(settings):
    with _api(settings) as api:
        first = api.get("/api/encode").json()
        put = api.put("/api/encode", json=SETTINGS)
        after = api.get("/api/encode").json()

    # A fresh database's row: vtuber-av1-uhq-cq46-srcfps before the options,
    # then the 30 fps cap migration 0037 gave the null cap.
    assert first == {
        "enabled": False,
        "tune": "uhq",
        "cq": 46,
        "fps_cap": 30,
        "still_fps_cap": 30,
        "motion_threshold": 1.0,
        "still_words": [
            "歌枠", "うたわく", "ASMR", "雑談", "karaoke", "singing stream", "free talk",
            "freetalk",
        ],
        "still_exclude_words": ["3D"],
        "summary": {
            "saved_bytes": 0, "source_bytes": 0, "held_bytes": 0,
            "succeeded": 0, "failed": 0, "open": 0,
        },
        "waiting": {"total": 0, "items": []},
        "finished": {"total": 0, "items": []},
    }  # fmt: skip
    assert put.is_success
    assert {k: after[k] for k in SETTINGS} == SETTINGS


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"cq": 0}, id="cq-0"),
        pytest.param({"cq": 64}, id="cq-64"),
        pytest.param({"fps_cap": 0}, id="fps-cap-0"),
        pytest.param({"still_fps_cap": -30}, id="still-fps-cap-negative"),
        pytest.param({"motion_threshold": 0}, id="motion-threshold-0"),
        pytest.param({"motion_threshold": -1}, id="motion-threshold-negative"),
        pytest.param({"tune": "ll"}, id="unknown-tune"),
    ],
)
def test_api_settings_refuse_out_of_range_values(settings, change):
    with _api(settings) as api:
        r = api.put("/api/encode", json={**SETTINGS, **change})
        after = api.get("/api/encode").json()

    assert r.status_code == 422
    assert after["enabled"] is False


def test_api_settings_store_word_lists_trimmed_and_deduplicated(settings):
    body = {
        **SETTINGS,
        "still_words": [" 歌枠 ", "asmr", "ASMR", ""],
        "still_exclude_words": ["3d"],
    }
    with _api(settings) as api:
        saved = api.put("/api/encode", json=body).json()

    assert (saved["still_words"], saved["still_exclude_words"]) == (["歌枠", "asmr"], ["3d"])


# Migration 0036 ----------------------------------------------------------------


@pytest.mark.own_database
@pytest.mark.parametrize(
    ("profile", "options", "back"),
    [
        pytest.param("vtuber-av1-hq-cq46-srcfps", ("hq", 46, None), "vtuber-av1-hq-cq46-srcfps",
                     id="hq-srcfps"),
        pytest.param("vtuber-av1-uhq-cq46-srcfps", ("uhq", 46, None),
                     "vtuber-av1-uhq-cq46-srcfps", id="uhq-srcfps"),
        pytest.param("vtuber-av1-hq-cq46", ("hq", 46, 30), "vtuber-av1-hq-cq46", id="hq-30"),
        pytest.param("vtuber-av1-uhq-cq46", ("uhq", 46, 30), "vtuber-av1-uhq-cq46", id="uhq-30"),
    ],
)  # fmt: skip
def test_migration_maps_the_profile_to_options_and_back(settings, profile, options, back):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0035")
    _sql(settings, f"UPDATE encode_settings SET enabled = true, profile = '{profile}'")

    command.upgrade(cfg, "0036")
    [row] = _sql_rows(
        settings,
        "SELECT enabled, tune, cq, fps_cap, still_fps_cap, still_exclude_words"
        " FROM encode_settings",
    )
    assert (row.enabled, (row.tune, row.cq, row.fps_cap)) == (True, options)
    assert (row.still_fps_cap, row.still_exclude_words) == (30, ["3D"])

    command.downgrade(cfg, "0035")
    [row] = _sql_rows(settings, "SELECT enabled, profile FROM encode_settings")
    assert (row.enabled, row.profile) == (True, back)


@pytest.mark.own_database
@pytest.mark.parametrize(
    ("options", "back"),
    [
        pytest.param(("uhq", 40, None), "vtuber-av1-uhq-cq46-srcfps", id="uhq-other-cq"),
        pytest.param(("hq", 46, 24), "vtuber-av1-hq-cq46", id="hq-other-cap"),
    ],
)
def test_migration_downgrade_maps_options_to_the_nearest_profile(settings, options, back):
    from alembic import command

    from savetube.db.migrate import alembic_config

    tune, cq, fps_cap = options
    _sql(
        settings,
        f"UPDATE encode_settings SET tune = '{tune}', cq = {cq},"
        f" fps_cap = {'NULL' if fps_cap is None else fps_cap}",
    )

    command.downgrade(alembic_config(settings.sqlalchemy_url), "0035")

    [row] = _sql_rows(settings, "SELECT profile FROM encode_settings")
    assert row.profile == back


# Migration 0037 ----------------------------------------------------------------


@pytest.mark.own_database
@pytest.mark.parametrize(("fps_cap", "after"), [(None, 30), (24, 24)])
def test_migration_measures_motion_and_caps_a_null_fps_cap(settings, fps_cap, after):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0036")
    _sql(settings, f"UPDATE encode_settings SET fps_cap = {'NULL' if fps_cap is None else fps_cap}")

    command.upgrade(cfg, "0037")

    [row] = _sql_rows(settings, "SELECT fps_cap, motion_threshold FROM encode_settings")
    assert (row.fps_cap, row.motion_threshold) == (after, 1.0)


# Migration 0038 ----------------------------------------------------------------


@pytest.mark.own_database
def test_migration_supersedes_a_result_no_longer_stored(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0037")
    # v1 stores an H.264 stream again after its encode, v2 still stores the result.
    _stored(settings, "v1")
    _stored(settings, "v2", info=_info("v2", video=AV1_SAME))
    _sql(
        settings,
        *[
            "INSERT INTO encode_job (video_id, state, profile, source_path, source_size)"
            f" VALUES ('{v}', 'succeeded', '{PROFILE}', '{_dir(v)}/{v}.mp4', 1000)"
            for v in ("v1", "v2")
        ],
    )

    command.upgrade(cfg, "0038")

    rows = _sql_rows(settings, "SELECT video_id, state FROM encode_job ORDER BY video_id")
    assert rows == [("v1", "superseded"), ("v2", "succeeded")]


def test_api_lists_jobs_and_requeues_only_failures(settings):
    _stored(settings)
    _sql(
        settings,
        "INSERT INTO encode_job (id, video_id, state, profile, source_path, source_size, reason,"
        f" log_tail, finished_at) VALUES (7, 'v1', 'failed', '{PROFILE}', '{_dir('v1')}/v1.mp4',"
        " 1000, 'sha256 mismatch', 'ffmpeg said no', now()),"
        f" (8, 'v1', 'queued', '{PROFILE}', '{_dir('v1')}/v1.mp4', 1000, NULL, NULL, NULL)",
    )

    def states(body):
        return {j["id"]: j["state"] for j in body["waiting"]["items"] + body["finished"]["items"]}

    with _api(settings) as api:
        listed = {j["id"]: j for j in api.get("/api/encode").json()["waiting"]["items"]}
        queued = api.post("/api/encode/jobs/8/requeue")
        unknown = api.post("/api/encode/jobs/99/requeue")
        requeued = api.post("/api/encode/jobs/7/requeue")
        again = api.post("/api/encode/jobs/7/requeue")
        after = states(api.get("/api/encode").json())

    failed = listed[7]
    assert {k: failed[k] for k in ("video_id", "title", "state", "profile", "source_size",
                                   "result_size", "reason", "log_tail")} == {
        "video_id": "v1", "title": "title v1", "state": "failed", "profile": PROFILE,
        "source_size": 1000, "result_size": None, "reason": "sha256 mismatch",
        "log_tail": "ffmpeg said no",
    }  # fmt: skip
    assert failed["created_at"] and failed["finished_at"]
    assert (queued.status_code, unknown.status_code, again.status_code) == (409, 404, 409)
    assert requeued.is_success
    # The requeue answers with the lists as they are after it.
    assert states(requeued.json()) == {7: "requeued", 8: "queued"}
    assert after == {7: "requeued", 8: "queued"}


def _job_rows(settings, rows):
    """Encode jobs as (id, video_id, state, source_size, result_size)."""
    values = ", ".join(
        f"({i}, '{v}', '{state}', '{PROFILE}', '{_dir(v)}/{v}.mp4', {source},"
        f" {'NULL' if result is None else result})"
        for i, v, state, source, result in rows
    )
    _sql(
        settings,
        "INSERT INTO encode_job (id, video_id, state, profile, source_path, source_size,"
        f" result_size) VALUES {values}",
    )


def test_api_summary_counts_savings_of_succeeded_results_only(settings):
    for v in ("v1", "v2", "v3", "v4", "v5", "v6", "v7"):
        _stored(settings, v)
    _job_rows(
        settings,
        [
            (1, "v1", "succeeded", 1000, 400),
            (2, "v2", "succeeded", 2000, 500),
            # A succeeded row without a result size saves nothing countable.
            (3, "v3", "succeeded", 5000, None),
            (4, "v4", "failed", 3000, 100),
            (5, "v5", "queued", 700, None),
            (6, "v6", "claimed", 800, None),
            (7, "v7", "cancelled", 900, None),
            (8, "v1", "lost", 900, 100),
        ],
    )
    # v1's original still waits in .originals/, v2's is deleted.
    _sql(
        settings,
        "INSERT INTO format_watch (video_id, original_path, original_until) VALUES"
        " ('v1', '.originals/v1/v1.mp4', now() + interval '7 days'),"
        " ('v2', NULL, NULL)",
    )

    with _api(settings) as api:
        summary = api.get("/api/encode").json()["summary"]

    assert summary == {
        "saved_bytes": 2100,
        "source_bytes": 3000,
        "held_bytes": 600,
        "succeeded": 3,
        "failed": 1,
        "open": 2,
    }


def test_api_pages_the_waiting_and_finished_jobs(settings):
    for i in range(1, 16):
        _stored(settings, f"v{i}")
    _job_rows(
        settings,
        [(i, f"v{i}", "queued" if i % 2 else "failed", 1000, None) for i in range(1, 13)]
        + [(i, f"v{i}", "succeeded", 1000, 400) for i in (13, 14, 15)],
    )

    with _api(settings) as api:
        first = api.get("/api/encode").json()
        second = api.get("/api/encode", params={"open_offset": 10}).json()
        finished = api.get("/api/encode", params={"finished_offset": 2, "limit": 2}).json()

    assert first["waiting"]["total"] == 12
    assert [j["id"] for j in first["waiting"]["items"]] == list(range(1, 11))
    assert [j["id"] for j in second["waiting"]["items"]] == [11, 12]
    assert first["finished"]["total"] == 3
    # The newest finished job comes first.
    assert [j["id"] for j in first["finished"]["items"]] == [15, 14, 13]
    assert [j["id"] for j in finished["finished"]["items"]] == [13]
