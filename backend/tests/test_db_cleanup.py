import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL, _dispatch, _events, _fields, _job_rows, _queue_row, _sql, _sql_rows

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    JobState,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


@pytest.mark.anyio
async def test_cleanup_deletes_partial_directories_no_job_wants_after_7_days(settings):
    import os
    import time

    from savetube.jobs import clean_incoming

    _job_rows(
        settings,
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('old', 'UC1', 'videos', 't', 'pending'), ('fresh', 'UC1', 'videos', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (42, 'record', 'running', 'busy2', 1), (43, 'archive', 'cancelled', 'old', 0)",
        "UPDATE video SET resume_format = '137+140' WHERE id IN ('old', 'fresh', 'v1')",
    )
    week = time.time() - 8 * 86400
    for name, stale in (("old", True), ("v1", True), ("fresh", False), ("42", True), ("7", True)):
        d = settings.incoming_root / name / ".tmp"
        d.mkdir(parents=True)
        f = d / "x.part"
        f.write_bytes(b"x")
        if stale:
            os.utime(f, (week, week))
            os.utime(d, (week, week))
            os.utime(d.parent, (week, week))
    engine = make_engine(settings.sqlalchemy_url)
    await clean_incoming(make_sessionmaker(engine), settings)
    await engine.dispose()
    # v1 has a queued archive job, 42 is a running recording, fresh is new.
    assert sorted(p.name for p in settings.incoming_root.iterdir()) == ["42", "fresh", "v1"]
    # A pin without its directory would only be cleared on the next run.
    pins = dict(_sql_rows(settings, "SELECT id, resume_format FROM video"))
    assert (pins["old"], pins["fresh"], pins["v1"]) == (None, "137+140", "137+140")


async def _clean_database(settings):
    from savetube.cleanup import clean_database

    engine = make_engine(settings.sqlalchemy_url)
    await clean_database(make_sessionmaker(engine))
    await engine.dispose()


@pytest.mark.anyio
async def test_cleanup_clears_progress_of_every_job_not_running(settings, caplog):
    import logging

    progress = "'{\"percent\": 50}'"
    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts, progress) VALUES"
        f" (2, 'archive', 'running', 'busy1', 1, {progress}),"
        f" (3, 'archive', 'succeeded', 'busy1', 1, {progress}),"
        f" (4, 'archive', 'interrupted', 'busy1', 0, {progress}),"
        f" (5, 'archive', 'failed', 'busy1', 1, {progress})",
        "UPDATE job SET progress = 'null' WHERE id = 1",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _clean_database(settings)
    # SQL NULL, not the JSON null a bare None writes to a JSONB column.
    rows = _sql_rows(settings, "SELECT id, progress FROM job WHERE progress IS NOT NULL")
    assert rows == [(2, {"percent": 50})]
    [done] = _events(caplog, "cleanup.done")
    assert done.levelno == logging.INFO
    assert _fields(done) == {"progress": 4, "queue_jobs": 0, "interrupted": 0, "live_seen": 0}

    # Nothing left to clear logs nothing.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _clean_database(settings)
    assert _events(caplog, "cleanup.done") == []


@pytest.mark.anyio
async def test_cleanup_deletes_finished_queue_jobs_after_30_days(settings, caplog):
    import logging

    ids = {
        (status, age): _queue_row(settings, status)
        for status in ("todo", "doing", "succeeded", "failed", "cancelled", "aborted")
        for age in (31, 29)
    }
    for (_, age), queue_job_id in ids.items():
        _sql(
            settings,
            f"UPDATE procrastinate_events SET at = now() - interval '{age} days'"
            f" WHERE job_id = {queue_job_id}",
        )
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _clean_database(settings)
    left = {r[0] for r in _sql_rows(settings, "SELECT id FROM procrastinate_jobs")}
    gone = {ids[s, 31] for s in ("succeeded", "failed", "cancelled", "aborted")}
    assert left == set(ids.values()) - gone
    events = {r[0] for r in _sql_rows(settings, "SELECT job_id FROM procrastinate_events")}
    assert events == left
    [done] = _events(caplog, "cleanup.done")
    assert _fields(done)["queue_jobs"] == 4


@pytest.mark.anyio
async def test_a_dispatched_job_whose_queue_job_was_deleted_goes_back_to_waiting(settings):
    queue_job_id = _queue_row(settings, "failed")
    _job_rows(
        settings,
        f"UPDATE job SET queue_job_id = {queue_job_id} WHERE id = 1",
        "UPDATE procrastinate_events SET at = now() - interval '31 days'",
    )
    await _clean_database(settings)
    assert _sql_rows(settings, "SELECT id FROM procrastinate_jobs") == []
    enqueued, jobs = await _dispatch(settings)
    assert enqueued == [1] and jobs[1].state is JobState.QUEUED


@pytest.mark.anyio
async def test_cleanup_deletes_interrupted_rows_30_days_after_the_video_succeeded(settings, caplog):
    import logging

    def finished(days):
        return f"now() - interval '{days} days'"

    _job_rows(
        settings,
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('old', 'UC1', 'videos', 't', 'complete'), ('recent', 'UC1', 'videos', 't', 'complete')",
        "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
        # old: succeeded 31 days ago, after an earlier success 60 days ago.
        f" (10, 'archive', 'interrupted', 'old', 0, {finished(62)}),"
        f" (11, 'archive', 'failed', 'old', 1, {finished(61)}),"
        f" (12, 'archive', 'cancelled', 'old', 0, {finished(61)}),"
        f" (13, 'archive', 'succeeded', 'old', 1, {finished(60)}),"
        f" (14, 'archive', 'interrupted', 'old', 0, {finished(32)}),"
        f" (15, 'archive', 'succeeded', 'old', 1, {finished(31)}),"
        f" (16, 'record', 'interrupted', 'old', 0, {finished(32)}),"
        # recent: succeeded 29 days ago.
        f" (20, 'archive', 'interrupted', 'recent', 0, {finished(40)}),"
        f" (21, 'archive', 'succeeded', 'recent', 1, {finished(29)}),"
        # v1: never succeeded, interrupted long ago.
        f" (30, 'archive', 'interrupted', 'v1', 0, {finished(90)}),"
        f" (31, 'archive', 'failed', 'v1', 1, {finished(90)})",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _clean_database(settings)
    left = sorted(r[0] for r in _sql_rows(settings, "SELECT id FROM job"))
    assert left == [1, 11, 12, 13, 15, 16, 20, 21, 30, 31]
    [done] = _events(caplog, "cleanup.done")
    assert _fields(done)["interrupted"] == 2


def test_migration_0008_turns_interrupted_jobs_into_cancelled_on_downgrade(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    _job_rows(
        settings,
        "UPDATE video SET resume_format = '251' WHERE id = 'v1'",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'interrupted', 'v1', 1)",
    )
    command.downgrade(alembic_config(settings.sqlalchemy_url), "0007")
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        states = dict(c.execute(sa.text("SELECT id, state FROM job")).all())
    engine.dispose()
    # The old code counts a failed job as an attempt, so an interrupted one
    # becomes cancelled.
    assert states == {1: "queued", 2: "cancelled"}
