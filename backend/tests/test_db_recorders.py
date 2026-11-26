import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL, _record_live, _sql

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import Job, JobState, MissReason, Video, VideoState
from savetube.recorders import settle_recorders, start_recorders

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

NOW = datetime(2026, 10, 23, 15, 57, tzinfo=UTC)
INFO = {"id": "v1", "title": "t", "channel_id": "UC1", "live_status": "is_live", "ext": "mkv"}


def _record_rows(settings, *jobs):
    """`jobs` holds (job id, state, scheduled_at SQL), each on its own video `v<id>`."""
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        *(
            "INSERT INTO video (id, channel_id, type, title, state) VALUES"
            f" ('v{i}', 'UC1', 'lives', 't', 'pending')"
            for i, _, _ in jobs
        ),
        *(
            "INSERT INTO job (id, kind, state, video_id, attempts, scheduled_at) VALUES"
            f" ({i}, 'record', '{state}', 'v{i}', 0, {at})"
            for i, state, at in jobs
        ),
    )


async def _rows(settings):
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
        videos = {v.id: v for v in await session.scalars(sa.select(Video))}
    await engine.dispose()
    return jobs, videos


async def _start(settings, existing=None):
    created = []

    async def create(rec):
        created.append(rec)
        return f"savetube-recorder-{rec.job_id}"

    engine = make_engine(settings.sqlalchemy_url)
    await start_recorders(make_sessionmaker(engine), settings, NOW, existing or {}, create=create)
    await engine.dispose()
    jobs, videos = await _rows(settings)
    return created, jobs, videos


async def _settle(settings, existing):
    engine = make_engine(settings.sqlalchemy_url)
    try:
        await settle_recorders(make_sessionmaker(engine), settings, NOW, existing)
    finally:
        await engine.dispose()
    return await _rows(settings)


def _outcome(settings, job_id, **outcome):
    """An outcome file as a recorder leaves it."""
    settings.incoming_root.mkdir(parents=True, exist_ok=True)
    (settings.incoming_root / f"{job_id}.json").write_text(json.dumps(outcome))


def _recorded(settings, job_id, video_id):
    """A finished recording of `video_id` in its working directory, and its outcome."""
    work = settings.incoming_root / str(job_id)
    work.mkdir(parents=True)
    (work / f"{video_id}.mkv").write_bytes(b"recording")
    _outcome(settings, job_id, outcome="recorded", info={**INFO, "id": video_id}, checks=None)


@pytest.mark.anyio
async def test_a_stream_starting_within_five_minutes_gets_its_recorder(settings):
    # 16:00 is three minutes after NOW.
    _record_rows(settings, (1, "queued", "'2026-10-23 16:00:00+00'"))
    created, jobs, videos = await _start(settings)
    assert [(r.job_id, r.video_id) for r in created] == [(1, "v1")]
    assert jobs[1].state is JobState.RUNNING and jobs[1].started_at == NOW
    assert jobs[1].attempts == 1
    assert videos["v1"].state is VideoState.DOWNLOADING


@pytest.mark.anyio
async def test_a_recorder_gets_the_channel_format_and_gives_up_a_day_past_the_start(settings):
    _record_rows(settings, (1, "queued", "'2026-10-23 16:00:00+00'"))
    _sql(settings, "UPDATE channel SET format = 'ba'")
    [rec], _, _ = await _start(settings)
    assert rec.format == "ba"
    assert rec.wait_until == datetime(2026, 10, 24, 16, tzinfo=UTC)
    assert (rec.profile, rec.member_profile, rec.members) == (None, None, False)


@pytest.mark.anyio
async def test_a_stream_an_hour_away_waits(settings):
    _record_rows(settings, (1, "queued", "'2026-10-23 17:00:00+00'"))
    created, jobs, _ = await _start(settings)
    assert created == []
    assert jobs[1].state is JobState.QUEUED


@pytest.mark.anyio
async def test_a_stream_found_live_and_a_late_one_start_now(settings):
    _record_rows(
        settings,
        (1, "queued", "NULL"),
        (2, "queued", "'2026-10-23 12:00:00+00'"),
    )
    created, _, _ = await _start(settings)
    assert sorted((r.job_id, r.video_id) for r in created) == [(1, "v1"), (2, "v2")]
    assert {r.job_id: r.wait_until for r in created}[1] is None


@pytest.mark.anyio
async def test_a_job_with_a_recorder_or_past_queued_gets_none(settings):
    _record_rows(
        settings,
        (1, "queued", "NULL"),
        (2, "running", "NULL"),
        (3, "succeeded", "NULL"),
        (4, "cancelled", "NULL"),
    )
    created, _, _ = await _start(settings, {1: "running"})
    assert created == []


@pytest.mark.anyio
async def test_a_refused_create_leaves_the_job_queued(settings):
    _record_rows(settings, (1, "queued", "NULL"))

    async def refused(rec):
        raise RuntimeError("forbidden")

    engine = make_engine(settings.sqlalchemy_url)
    await start_recorders(make_sessionmaker(engine), settings, NOW, {}, create=refused)
    await engine.dispose()
    jobs, _ = await _rows(settings)
    assert jobs[1].state is JobState.QUEUED and jobs[1].attempts == 0


def _streams_while_the_database_is_down(url, params, on_format):
    """yt-dlp recording v1: progress, then the merged file."""
    home = Path(params["paths"]["home"])
    [hook] = params["progress_hooks"]
    on_format({"format_id": "299+140"})
    for done in (1024, 2048):
        hook({"status": "downloading", "downloaded_bytes": done, "info_dict": {"format_id": "299"}})
    media = home / "v1.mkv"
    media.write_bytes(b"recording")
    return {**INFO, "requested_downloads": [{"filepath": str(media)}]}


@pytest.mark.anyio
async def test_a_recording_made_while_the_database_is_down_is_stored(settings, monkeypatch):
    from savetube.jobs import runners

    monkeypatch.setattr(runners, "PROGRESS_SEND_SECONDS", 0)
    monkeypatch.setattr(runners, "download", _streams_while_the_database_is_down)
    _record_rows(settings, (1, "queued", "NULL"))

    _, status = await _record_live(settings, runners.in_process)

    jobs, videos = await _rows(settings)
    assert status == "succeeded"
    assert jobs[1].state is JobState.SUCCEEDED
    assert videos["v1"].state is VideoState.COMPLETE
    stored = settings.archive_root / videos["v1"].dir_path / "v1.mkv"
    assert stored.read_bytes() == b"recording"
    assert not (settings.incoming_root / "1.json").exists()


@pytest.mark.anyio
async def test_an_outcome_the_database_refused_is_applied_on_the_next_tick(settings, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    _record_rows(settings, (1, "running", "NULL"))
    _recorded(settings, 1, "v1")
    commit = AsyncSession.commit
    refusals = [ConnectionError("the database is down")]

    async def flaky(self):
        if refusals:
            raise refusals.pop()
        await commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky)
    with pytest.raises(ConnectionError):
        await _settle(settings, {1: "succeeded"})
    jobs, _ = await _rows(settings)
    assert jobs[1].state is JobState.RUNNING
    assert (settings.incoming_root / "1.json").exists()

    jobs, videos = await _settle(settings, {1: "succeeded"})
    assert jobs[1].state is JobState.SUCCEEDED
    assert videos["v1"].state is VideoState.COMPLETE
    stored = settings.archive_root / videos["v1"].dir_path / "v1.mkv"
    assert stored.read_bytes() == b"recording"
    assert not (settings.incoming_root / "1.json").exists()


@pytest.mark.anyio
async def test_a_stream_that_never_started_ends_missed(settings):
    from savetube.jobs.recording import NOT_STARTED_LOG

    _record_rows(settings, (1, "running", "NULL"))
    _outcome(settings, 1, outcome="not_started")
    jobs, videos = await _settle(settings, {1: "succeeded"})
    assert jobs[1].state is JobState.MISSED and jobs[1].miss_reason is None
    assert jobs[1].log_tail == NOT_STARTED_LOG and jobs[1].finished_at == NOW
    assert videos["v1"].state is VideoState.FAILED
    assert not (settings.incoming_root / "1.json").exists()


@pytest.mark.anyio
async def test_a_recorder_that_failed_every_retry_fails_its_job_with_the_error(settings):
    _record_rows(settings, (1, "running", "NULL"))
    _outcome(settings, 1, outcome="failed", error="the live ended badly")
    jobs, videos = await _settle(settings, {1: "failed"})
    assert jobs[1].state is JobState.FAILED and jobs[1].log_tail == "the live ended badly"
    assert jobs[1].finished_at == NOW
    assert videos["v1"].state is VideoState.FAILED


@pytest.mark.anyio
async def test_an_ended_recorder_with_no_outcome_marks_its_open_job_missed(settings):
    _record_rows(
        settings,
        (1, "running", "NULL"),
        (2, "queued", "NULL"),
        (3, "failed", "NULL"),
        (4, "running", "NULL"),
        (5, "running", "NULL"),
    )
    jobs, videos = await _settle(
        settings, {1: "failed", 2: "failed", 3: "failed", 4: "running", 5: "succeeded"}
    )
    for i in (1, 2, 5):
        assert jobs[i].state is JobState.MISSED
        assert jobs[i].miss_reason is MissReason.RECORDER_FAILED
        assert jobs[i].finished_at == NOW
        assert videos[f"v{i}"].state is VideoState.FAILED
    assert jobs[3].state is JobState.FAILED and jobs[3].miss_reason is None
    assert jobs[4].state is JobState.RUNNING
    assert videos["v4"].state is VideoState.PENDING


@pytest.mark.anyio
async def test_a_running_job_whose_recorder_is_gone_ends_missed(settings):
    _record_rows(settings, (1, "running", "NULL"))
    _sql(settings, "UPDATE job SET started_at = '2026-10-23 15:00:00+00' WHERE id = 1")
    jobs, videos = await _settle(settings, {})
    assert jobs[1].state is JobState.MISSED
    assert jobs[1].miss_reason is MissReason.RECORDER_FAILED
    assert jobs[1].finished_at == NOW
    assert videos["v1"].state is VideoState.FAILED


@pytest.mark.anyio
async def test_a_recorder_started_this_tick_is_not_settled_before_it_is_listed(settings):
    # NOW is 15:57, and the listing predates this tick's start.
    _record_rows(settings, (1, "running", "NULL"))
    _sql(settings, "UPDATE job SET started_at = '2026-10-23 15:57:00+00' WHERE id = 1")
    jobs, videos = await _settle(settings, {})
    assert jobs[1].state is JobState.RUNNING and jobs[1].finished_at is None
    assert videos["v1"].state is VideoState.PENDING


@pytest.mark.anyio
async def test_the_outcome_of_a_job_no_longer_open_is_deleted_unapplied(settings):
    _record_rows(settings, (1, "cancelled", "NULL"))
    _recorded(settings, 1, "v1")
    jobs, videos = await _settle(settings, {1: "succeeded"})
    assert jobs[1].state is JobState.CANCELLED
    assert videos["v1"].state is VideoState.PENDING
    assert not (settings.incoming_root / "1.json").exists()


@pytest.mark.anyio
async def test_a_recorder_refused_by_level_records_the_level_on_its_video(settings):
    error = (
        "ERROR: [youtube] v1: This video is available to this channel's members on level:"
        " KMNCLUB Premium (or any higher level). Join this channel to get access to"
        " members-only content and other exclusive perks."
    )
    _record_rows(settings, (1, "running", "NULL"))
    _outcome(settings, 1, outcome="failed", error=error)
    jobs, videos = await _settle(settings, {1: "failed"})
    assert jobs[1].state is JobState.FAILED and jobs[1].log_tail == error
    assert videos["v1"].members_level == "KMNCLUB Premium"
