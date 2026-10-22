import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    _api,
    _cancel,
    _events,
    _fields,
    _finished,
    _home,
    _job_rows,
    _no_probe,
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


def _staging(settings):
    return settings.model_copy(update={"staging_root": settings.archive_root.parent / "staging"})


@pytest.mark.anyio
async def test_a_staged_download_waits_on_staging_outside_the_tree(settings):
    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    staged = _staging(settings)
    runner = _Steps(_finished())

    jobs, video = await _run_steps(staged, runner)

    assert _home(runner.calls[0]) == staged.staging_root / "incoming" / "v1"
    assert jobs[1].state is JobState.SUCCEEDED
    assert (video.state, video.size_bytes) == (VideoState.STAGED, 5)
    assert (staged.staging_root / "ready" / "v1" / "v1.webm").read_bytes() == b"m" * 5
    assert video.dir_path and not (settings.archive_root / video.dir_path).exists()


@pytest.mark.anyio
async def test_poll_counts_a_staged_video_as_stored(settings):
    from savetube.poll import poll_source

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Chan', 'Chan [UC1]')",
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@chan/videos', 'tab', true, 360)",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('done', 'UC1', 'videos', 'd', 'staged')",
    )
    listing = {
        "channel_id": "UC1",
        "channel": "Chan",
        "uploader_id": "@chan",
        "entries": [{"id": "done", "title": "Done"}],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await poll_source(
        sessionmaker, 1, settings, lister=lambda url, c: listing, cancel=_cancel, prober=_no_probe
    )
    async with sessionmaker() as session:
        assert list(await session.scalars(sa.select(Job))) == []
        assert (await session.get(Video, "done")).state is VideoState.STAGED
    await engine.dispose()


def _staged_rows(settings, *, waited="2 hours", files=True, extra=()):
    """Video s1, staged `waited` ago, with its files in the ready directory."""
    staged = _staging(settings)
    staged.staging_root.mkdir(parents=True)
    if files:
        ready = staged.staging_root / "ready" / "s1"
        ready.mkdir(parents=True)
        (ready / "s1.mkv").write_bytes(b"x" * 10)
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, dir_path, size_bytes,"
        " completed_at) VALUES ('s1', 'UC1', 'videos', 't', 'staged',"
        f" 'A [UC1]/videos/s1', 10, now() - interval '{waited}')",
        *extra,
    )
    return staged


async def _move(staged):
    from savetube.staging import move_staged

    engine = make_engine(staged.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await move_staged(sessionmaker, staged)
    async with sessionmaker() as session:
        video = await session.get(Video, "s1")
    await engine.dispose()
    return video


@pytest.mark.anyio
async def test_the_mover_moves_staged_videos_into_the_tree_after_an_idle_hour(settings, caplog):
    import logging

    staged = _staged_rows(settings)
    with caplog.at_level(logging.INFO, logger="savetube"):
        video = await _move(staged)

    assert video.state is VideoState.COMPLETE
    assert (settings.archive_root / "A [UC1]/videos/s1/s1.mkv").read_bytes() == b"x" * 10
    assert not (staged.staging_root / "ready" / "s1").exists()
    assert list(settings.incoming_root.iterdir()) == []
    [start] = _events(caplog, "staging.move")
    assert (_fields(start)["reason"], _fields(start)["count"]) == ("idle", 1)
    assert len(_events(caplog, "staging.moved")) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("waited", "extra"),
    [
        ("5 minutes", ()),
        (
            "2 hours",
            (
                "INSERT INTO job (kind, state, video_id, attempts)"
                " VALUES ('archive', 'running', 's1', 1)",
            ),
        ),
    ],
    ids=["recent", "downloading"],
)
async def test_the_mover_waits_while_the_batch_is_small(settings, waited, extra):
    staged = _staged_rows(settings, waited=waited, extra=extra)

    video = await _move(staged)

    assert video.state is VideoState.STAGED
    assert (staged.staging_root / "ready" / "s1" / "s1.mkv").is_file()
    assert not (settings.archive_root / "A [UC1]").exists()


@pytest.mark.anyio
async def test_the_mover_finishes_a_move_cut_off_after_the_rename(settings):
    staged = _staged_rows(settings, files=False)
    final = settings.archive_root / "A [UC1]/videos/s1"
    final.mkdir(parents=True)
    (final / "s1.mkv").write_bytes(b"x" * 10)

    video = await _move(staged)

    assert video.state is VideoState.COMPLETE
    assert (final / "s1.mkv").is_file()


@pytest.mark.anyio
async def test_the_mover_requeues_a_staged_video_whose_files_are_gone(settings, caplog):
    import logging

    staged = _staged_rows(settings, files=False)
    with caplog.at_level(logging.INFO, logger="savetube"):
        video = await _move(staged)

    assert video.state is VideoState.PENDING
    assert len(_events(caplog, "staging.missing")) == 1


def test_migration_0014_turns_staged_videos_pending_on_downgrade(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('s1', 'UC1', 'videos', 't', 'staged'), ('c1', 'UC1', 'videos', 't', 'complete')",
    )
    command.downgrade(alembic_config(settings.sqlalchemy_url), "0013")
    rows = dict(_sql_rows(settings, "SELECT id, state FROM video"))
    # The old code finds no staging, so the poll downloads the video again.
    assert rows == {"s1": "pending", "c1": "complete"}


@pytest.mark.anyio
async def test_the_mover_touches_nothing_while_staging_is_unmounted(settings, caplog):
    import logging
    import shutil

    staged = _staged_rows(settings, files=False)
    shutil.rmtree(staged.staging_root)
    with caplog.at_level(logging.INFO, logger="savetube"):
        video = await _move(staged)

    assert video.state is VideoState.STAGED
    assert len(_events(caplog, "staging.unmounted")) == 1


def test_api_counts_the_videos_waiting_on_staging(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, size_bytes) VALUES"
        " ('s1', 'UC1', 'videos', 't', 'staged', 10), ('s2', 'UC1', 'videos', 't', 'staged', 5),"
        " ('c1', 'UC1', 'videos', 't', 'complete', 100)",
    )
    with _api(settings) as client:
        assert client.get("/api/staging").json() == {
            "count": 2,
            "bytes": 15,
            "move_requested": False,
        }
        assert client.post("/api/staging/move").json()["move_requested"] is True
        assert client.get("/api/staging").json()["move_requested"] is True


@pytest.mark.anyio
async def test_a_move_request_moves_a_small_batch_during_a_download(settings, caplog):
    import logging

    staged = _staged_rows(
        settings,
        waited="5 minutes",
        extra=(
            "INSERT INTO job (kind, state, video_id, attempts)"
            " VALUES ('archive', 'running', 's1', 1)",
            "UPDATE download_settings SET move_requested_at = now()",
        ),
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        video = await _move(staged)

    assert video.state is VideoState.COMPLETE
    [start] = _events(caplog, "staging.move")
    assert _fields(start)["reason"] == "manual"
    assert _sql_rows(settings, "SELECT move_requested_at FROM download_settings") == [(None,)]
