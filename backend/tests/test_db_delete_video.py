"""Deleting a stored video from the web UI (docs/plan/storage.md, Deleting a
video): its files go, its row stays as `deleted`, and nothing queues it again."""

import asyncio
import logging

import httpx2
import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    LIVE_NOW,
    NASA,
    _api,
    _cancel,
    _events,
    _feed,
    _fields,
    _no_probe,
    _no_streams,
    _record_jobs,
    _sql,
    _sql_rows,
    _watch,
)

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import Job, JobKind
from savetube.integrity import Tools
from savetube.scheduler_api import Requests, create_app

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

DIR = "A [UC1]/lives/2026-10-30 APEX [v1]"


def _stored(settings, *extra, state="complete", dir_path=DIR):
    """Video v1 with its directory in the tree and a kept original."""
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes)"
        " VALUES (1, 'https://www.youtube.com/@a/videos', 'tab', 'UC1', true, 360)",
        "INSERT INTO video (id, channel_id, type, title, state, dir_path, size_bytes,"
        " completed_at, verified_at, verified_checks) VALUES"
        f" ('v1', 'UC1', 'lives', 'APEX', '{state}', '{dir_path}', 12, now(), now(), '{{size}}')",
        "INSERT INTO video_source (video_id, source_id) VALUES ('v1', 1)",
        "INSERT INTO format_watch (video_id, started_at, original_path) VALUES"
        " ('v1', now(), '.originals/v1/v1.mkv')",
        *extra,
    )
    video = settings.archive_root / DIR
    video.mkdir(parents=True)
    (video / "v1.mkv").write_bytes(b"x" * 10)
    (video / "v1.info.json").write_bytes(b"{}")
    original = settings.archive_root / ".originals" / "v1"
    original.mkdir(parents=True)
    (original / "v1.mkv").write_bytes(b"o" * 5)
    return video, original


def _video_row(settings):
    [row] = _sql_rows(
        settings,
        "SELECT state, dir_path, size_bytes, completed_at, verified_at FROM video WHERE id = 'v1'",
    )
    return tuple(row)


class _Scheduler:
    """The scheduler's API behind the web's outbound httpx2 calls."""

    def __init__(self, settings):
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from savetube.db import PSYCOPG_CONNECT_ARGS

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
        )
        self.urls = []

    async def handle(self, request):
        self.urls.append((request.method, str(request.url)))
        return await httpx2.ASGITransport(app=self.app).handle_async_request(request)


def _delete(settings, monkeypatch, video_id="v1"):
    scheduler = _Scheduler(settings)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)
    with _api(settings) as api:
        r = api.delete(f"/api/videos/{video_id}")
        listed = api.get("/api/videos").json()
    return r, listed, scheduler


def test_delete_removes_the_files_and_keeps_a_deleted_row(settings, monkeypatch, caplog):
    video, original = _stored(settings)

    with caplog.at_level(logging.INFO, logger="savetube"):
        r, listed, scheduler = _delete(settings, monkeypatch)

    assert r.status_code == 204
    assert scheduler.urls == [("DELETE", f"{settings.scheduler_url}/videos/v1")]
    assert not video.exists() and not original.exists()
    # The channel directory stays: other videos live there.
    assert (settings.archive_root / "A [UC1]" / "lives").is_dir()
    assert _video_row(settings) == ("deleted", None, None, None, None)
    assert _sql_rows(settings, "SELECT source_id FROM video_source WHERE video_id = 'v1'") == [(1,)]
    [watch] = _sql_rows(settings, "SELECT ended_at IS NOT NULL, original_path FROM format_watch")
    assert tuple(watch) == (True, None)
    assert listed == {"total": 0, "items": []}
    [line] = _events(caplog, "video.deleted")
    # 10 + 2 bytes in the tree, 5 in .originals.
    assert (_fields(line)["video"], _fields(line)["size"]) == ("v1", "17B")


def test_delete_removes_the_staged_copy(settings, monkeypatch):
    staged = settings.model_copy(update={"staging_root": settings.archive_root.parent / "staging"})
    _stored(staged, state="staged")
    ready = staged.ready_root / "v1"
    ready.mkdir(parents=True)
    (ready / "v1.mkv").write_bytes(b"s" * 3)

    r, *_ = _delete(staged, monkeypatch)

    assert r.status_code == 204
    assert not ready.exists() and not (staged.archive_root / DIR).exists()
    assert _video_row(staged)[0] == "deleted"


def test_delete_refuses_a_directory_outside_the_archive_root(settings, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.mkv").write_bytes(b"k")
    video, original = _stored(settings, dir_path="../outside")

    r, *_ = _delete(settings, monkeypatch)

    assert r.status_code == 409
    assert (outside / "keep.mkv").is_file()
    assert original.is_dir()
    assert _video_row(settings)[:3] == ("complete", "../outside", 12)


@pytest.mark.parametrize(
    "busy",
    [
        "INSERT INTO job (kind, state, video_id, attempts, format_id)"
        " VALUES ('replace', 'queued', 'v1', 0, '616')",
        "INSERT INTO job (kind, state, video_id, attempts) VALUES ('record', 'running', 'v1', 1)",
        "INSERT INTO encode_job (video_id, state, profile, source_path, source_size)"
        " VALUES ('v1', 'claimed', 'vtuber-av1-cq46', 'x.mkv', 10)",
    ],
)
def test_delete_refuses_a_video_a_job_holds(settings, monkeypatch, busy):
    video, original = _stored(settings, busy)

    r, listed, *_ = _delete(settings, monkeypatch)

    assert r.status_code == 409
    assert (video / "v1.mkv").is_file() and original.is_dir()
    assert _video_row(settings)[0] == "complete"
    assert listed["total"] == 1


def test_delete_answers_404_for_an_unknown_video(settings, monkeypatch):
    r, *_ = _delete(settings, monkeypatch, video_id="nope")

    assert r.status_code == 404


def test_delete_refuses_a_video_already_deleted(settings, monkeypatch):
    _stored(settings)

    _delete(settings, monkeypatch)
    again, *_ = _delete(settings, monkeypatch)

    assert again.status_code == 409


@pytest.mark.anyio
async def test_poll_queues_no_archive_job_for_a_deleted_video(settings):
    from savetube.poll import poll_source

    _stored(settings, "UPDATE video SET state = 'deleted', dir_path = NULL WHERE id = 'v1'")
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    listing = {
        "channel_id": "UC1",
        "channel": "A",
        "uploader_id": "@a",
        "entries": [{"id": "v1", "title": "APEX"}, {"id": "v2", "title": "Other"}],
    }

    await poll_source(
        sessionmaker, 1, settings, lister=lambda u, c: listing, cancel=_cancel, prober=_no_probe
    )

    async with sessionmaker() as session:
        queued = list(
            await session.scalars(sa.select(Job.video_id).where(Job.kind == JobKind.ARCHIVE))
        )
    await engine.dispose()
    assert queued == ["v2"]
    assert _video_row(settings)[0] == "deleted"


@pytest.mark.anyio
async def test_live_check_queues_no_record_job_for_a_deleted_video(settings):
    from savetube.live import check_channel

    _watch(settings)
    _sql(
        settings,
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('{LIVE_NOW['id']}', '{NASA}', 'lives', 't', 'deleted')",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    created = await check_channel(
        sessionmaker,
        NASA,
        fetch_feed=lambda c: _feed(),
        probe=lambda v: pytest.fail("nothing in the feed to probe"),
        probe_live=lambda c: LIVE_NOW,
        list_streams=_no_streams,
    )

    assert created == [] and await _record_jobs(sessionmaker) == []
    await engine.dispose()
