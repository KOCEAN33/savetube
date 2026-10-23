from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL, _sql

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import Job, JobState, MissReason, Video, VideoState
from savetube.recorders import start_recorders

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

NOW = datetime(2026, 10, 23, 15, 57, tzinfo=UTC)


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


async def _start(settings, recorders=None):
    created = []

    async def listed():
        return recorders or {}

    async def create(job_id, video_id):
        created.append((job_id, video_id))
        return f"savetube-recorder-{job_id}"

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await start_recorders(sessionmaker, NOW, recorders=listed, create=create)
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
        videos = {v.id: v for v in await session.scalars(sa.select(Video))}
    await engine.dispose()
    return created, jobs, videos


@pytest.mark.anyio
async def test_a_stream_starting_within_five_minutes_gets_its_recorder(settings):
    # 16:00 is three minutes after NOW.
    _record_rows(settings, (1, "queued", "'2026-10-23 16:00:00+00'"))
    created, _, _ = await _start(settings)
    assert created == [(1, "v1")]


@pytest.mark.anyio
async def test_a_stream_an_hour_away_waits(settings):
    _record_rows(settings, (1, "queued", "'2026-10-23 17:00:00+00'"))
    created, _, _ = await _start(settings)
    assert created == []


@pytest.mark.anyio
async def test_a_stream_found_live_and_a_late_one_start_now(settings):
    _record_rows(
        settings,
        (1, "queued", "NULL"),
        (2, "queued", "'2026-10-23 12:00:00+00'"),
    )
    created, _, _ = await _start(settings)
    assert sorted(created) == [(1, "v1"), (2, "v2")]


@pytest.mark.anyio
async def test_a_job_with_a_recorder_or_past_queued_gets_none(settings):
    _record_rows(
        settings,
        (1, "queued", "NULL"),
        (2, "running", "NULL"),
        (3, "succeeded", "NULL"),
        (4, "cancelled", "NULL"),
    )
    created, _, _ = await _start(settings, recorders={1: False})
    assert created == []


@pytest.mark.anyio
async def test_a_failed_recorder_marks_its_open_job_missed(settings):
    _record_rows(
        settings,
        (1, "running", "NULL"),
        (2, "queued", "NULL"),
        (3, "failed", "NULL"),
        (4, "running", "NULL"),
    )
    created, jobs, videos = await _start(settings, recorders={1: True, 2: True, 3: True, 4: False})
    assert created == []
    for i in (1, 2):
        assert jobs[i].state is JobState.MISSED
        assert jobs[i].miss_reason is MissReason.RECORDER_FAILED
        assert jobs[i].finished_at == NOW
        assert videos[f"v{i}"].state is VideoState.FAILED
    assert jobs[3].state is JobState.FAILED and jobs[3].miss_reason is None
    assert jobs[4].state is JobState.RUNNING
    assert videos["v4"].state is VideoState.PENDING
