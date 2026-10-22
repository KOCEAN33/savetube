"""Runs against a real Postgres. SAVETUBE_TEST_DATABASE_URL is an admin URL whose
role has CREATEDB. Each run creates and drops its own database,
savetube_test_<unix seconds>_<hex>, so concurrent runs against one server do not collide.
Setup also drops databases of that pattern older than 24 hours, which killed runs leave behind."""

import asyncio
import json
import os
import re
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from savetube.config import Settings
from savetube.db import make_engine, make_sessionmaker
from savetube.db.migrate import migrate
from savetube.db.models import (
    Channel,
    Job,
    JobKind,
    JobState,
    ListingEntry,
    Source,
    SourceKind,
    Video,
    VideoSource,
    VideoState,
)
from savetube.layout import VideoType
from savetube.pipeline import store
from savetube.web.app import create_app
from savetube.web.auth import User, current_user

ADMIN_URL = os.environ.get("SAVETUBE_TEST_DATABASE_URL", "")
TEST_DB = f"savetube_test_{int(time.time())}_{secrets.token_hex(6)}"
TEST_DB_RE = re.compile(r"savetube_test_(\d+)_[0-9a-f]+")
STALE_AFTER_S = 24 * 3600

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


@pytest.fixture(scope="module")
def test_dsn():
    admin = Settings(database_url=ADMIN_URL)
    engine = sa.create_engine(admin.sqlalchemy_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        query = r"SELECT datname FROM pg_database WHERE datname LIKE 'savetube\_test\_%'"
        names = c.execute(sa.text(query)).scalars().all()
        cutoff = time.time() - STALE_AFTER_S
        for name in names:
            m = TEST_DB_RE.fullmatch(name)
            if m and int(m.group(1)) < cutoff:
                c.execute(sa.text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        c.execute(sa.text(f"CREATE DATABASE {TEST_DB}"))
    yield urlunsplit(urlsplit(ADMIN_URL)._replace(path=f"/{TEST_DB}"))
    with engine.connect() as c:
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    engine.dispose()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path, test_dsn):
    s = Settings(
        database_url=test_dsn,
        archive_root=tmp_path / "archive",
        session_secret="s",
        oidc_client_id="c",
        oidc_issuer="https://issuer.test",
        public_url="http://testserver",
    )
    engine = sa.create_engine(s.sqlalchemy_url)
    with engine.begin() as c:
        c.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    engine.dispose()
    migrate(s)
    return s


INFO = {
    "id": "dQw4w9WgXcQ",
    "title": "첫 방송 / part 1",
    "channel_id": "UC1",
    "channel": "Renamed Channel",
    "upload_date": "20260315",
    "duration": 3600,
    "live_status": "was_live",
    "availability": "public",
    "format_id": "299+251",
}


@pytest.mark.anyio
async def test_store_moves_download_and_records_video(settings):
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        session.add(Channel(id="UC1", name="Original", dir_name="Original [UC1]"))
        session.add(Video(id=INFO["id"], channel_id="UC1", type=VideoType.VIDEOS, title="?"))
        # No relationship() orders these inserts, so the FK targets go first.
        await session.flush()
        job = Job(kind=JobKind.RECORD, video_id=INFO["id"])
        session.add(job)
        await session.commit()
        job_id = job.id

    incoming = settings.incoming_root / str(job_id)
    (incoming / ".tmp").mkdir(parents=True)
    (incoming / ".tmp" / "frag1").write_bytes(b"leftover")
    (incoming / "dQw4w9WgXcQ.mkv").write_bytes(b"x" * 1000)
    (incoming / "dQw4w9WgXcQ.info.json").write_bytes(b"{}")

    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        await store(
            session, job=job, info=INFO, incoming=incoming, archive_root=settings.archive_root
        )
        await session.commit()

    # The directory name was fixed at first add; the YouTube rename does not move it.
    final = (
        settings.archive_root
        / "Original [UC1]"
        / "lives"
        / "2026-03-15 첫 방송 ⧸ part 1 [dQw4w9WgXcQ]"
    )
    assert sorted(p.name for p in final.iterdir()) == ["dQw4w9WgXcQ.info.json", "dQw4w9WgXcQ.mkv"]
    assert not incoming.exists()

    async with sessionmaker() as session:
        video = await session.get(Video, INFO["id"])
        job = await session.get(Job, job_id)
        channel = await session.get(Channel, "UC1")
        assert video.state is VideoState.COMPLETE
        assert video.type is VideoType.LIVES
        assert video.size_bytes == 1002
        assert video.dir_path == "Original [UC1]/lives/2026-03-15 첫 방송 ⧸ part 1 [dQw4w9WgXcQ]"
        assert video.info["format_id"] == "299+251"
        assert job.state is JobState.SUCCEEDED and job.finished_at is not None
        assert channel.dir_name == "Original [UC1]"
    await engine.dispose()


def test_api_reports_only_stored_videos(settings):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.begin() as c:
        c.execute(
            sa.text("INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')")
        )
        c.execute(
            sa.text(
                "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes)"
                " VALUES ('https://www.youtube.com/@a/videos', 'tab', 'UC1', true, 360)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO video"
                " (id, channel_id, type, title, duration, size_bytes, state, completed_at) VALUES"
                "         ('v1', 'UC1', 'videos', 'alpha_1', 60, 100, 'complete', now()),"
                "        ('v2', 'UC1', 'lives', 'beta', 120, 300, 'complete', now()),"
                "        ('v3', 'UC1', 'videos', 'gamma', 999, 999, 'pending', NULL)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO storage_sample"
                " VALUES (now() - interval '1 hour', 10, 4), (now(), 10, 3)"
            )
        )
    engine.dispose()

    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    with TestClient(app) as client:
        summary = client.get("/api/stats/summary").json()
        assert summary["total_bytes"] == 400
        assert summary["video_count"] == 2
        assert summary["duration_seconds"] == 180
        assert summary["free_bytes"] == 3
        assert sorted((t["type"], t["bytes"], t["count"]) for t in summary["by_type"]) == [
            ("lives", 300, 1),
            ("videos", 100, 1),
        ]
        assert summary["by_channel"] == [
            {"channel_id": "UC1", "name": "A", "bytes": 400, "count": 2}
        ]

        daily = client.get("/api/stats/daily").json()
        assert [(d["bytes"], d["count"]) for d in daily] == [(400, 2)]

        assert client.get("/api/channels").json()[0]["video_count"] == 2

        page = client.get("/api/videos", params={"type": "videos"}).json()
        assert page["total"] == 2
        # `_` in the query is literal, not a LIKE wildcard.
        assert client.get("/api/videos", params={"q": "a_1"}).json()["total"] == 1
        assert client.get("/api/videos", params={"q": "a_"}).json()["total"] == 1
        assert client.get("/api/videos/v2").json()["type"] == "lives"
        assert client.get("/api/videos/nope").status_code == 404


def _sql(settings, *statements):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.begin() as c:
        for s in statements:
            c.execute(sa.text(s))
    engine.dispose()


TAB_LISTING = {
    "channel_id": "UC1",
    "channel": "Chan",
    "uploader_id": "@chan",
    "entries": [
        {"id": "new", "title": "New", "timestamp": 1789948800},  # 2026-09-21
        {"id": "upcoming", "title": "Soon", "live_status": "is_upcoming"},
        {"id": "old", "title": "Old", "timestamp": 1735689600},  # 2025-01-01
        {"id": "undated", "title": "Undated"},
        {"id": "past_live", "title": "Was live", "live_status": "was_live"},
        {"id": "done", "title": "Done"},
        {"id": "gave_up", "title": "Failed thrice"},
        {"id": "retry", "title": "Failed once"},
        {"id": "queued", "title": "Queued"},
    ],
}


@pytest.mark.anyio
async def test_poll_queues_unstored_videos_once(settings):
    from savetube.poll import due_sources, poll_source

    _sql(
        settings,
        # The poll finds this channel by the listing's channel id.
        "INSERT INTO channel (id, name, dir_name, date_after) VALUES"
        " ('UC1', 'Chan', 'Chan [UC1]', '2026-01-01')",
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@chan/videos', 'tab', true, 360),"
        " (2, 'https://www.youtube.com/playlist?list=PL1', 'playlist', true, 360),"
        " (3, 'https://www.youtube.com/@off/videos', 'tab', false, 360)",
        "UPDATE source SET other_channels = true WHERE id = 2",
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC0', 'Zero', 'Zero [UC0]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('done', 'UC0', 'videos', 'd', 'complete'), ('gave_up', 'UC0', 'videos', 'g', 'failed'),"
        " ('retry', 'UC0', 'videos', 'r', 'failed'), ('queued', 'UC0', 'videos', 'q', 'pending')",
        "INSERT INTO job (kind, state, video_id, attempts) VALUES"
        " ('archive', 'failed', 'gave_up', 1), ('archive', 'failed', 'gave_up', 1),"
        " ('archive', 'failed', 'gave_up', 1), ('archive', 'failed', 'retry', 1),"
        " ('archive', 'queued', 'queued', 0)",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        assert await due_sources(session, datetime.now(UTC)) == [1, 2]

    await poll_source(
        sessionmaker,
        1,
        settings,
        lister=lambda url, c: TAB_LISTING,
        cancel=_cancel,
        prober=_no_probe,
    )

    async with sessionmaker() as session:
        # Jobs 1-5 existed before the poll.
        jobs = {j.video_id: j for j in await session.scalars(sa.select(Job).where(Job.id > 5))}
        # The tab decides what is kept, so a past live on it is archived too.
        # `old` is before the channel's date cutoff.
        assert sorted(jobs) == ["new", "past_live", "retry", "undated"]
        # A new job waits for the dispatcher, with no queue job of its own.
        assert all(j.state is JobState.QUEUED and j.queue_job_id is None for j in jobs.values())
        assert (await session.get(Video, "new")).state is VideoState.PENDING
        # A requeued failure waits as pending. One out of attempts stays failed.
        assert (await session.get(Video, "retry")).state is VideoState.PENDING
        assert (await session.get(Video, "gave_up")).state is VideoState.FAILED
        assert (await session.get(Video, "new")).channel_id == "UC1"
        assert await session.get(Video, "upcoming") is None
        assert (await session.get(Channel, "UC1")).dir_name == "Chan [UC1]"
        source = await session.get(Source, 1)
        assert source.channel_id == "UC1"
        assert source.last_polled_at is not None and source.last_error is None
        linked = set(
            await session.scalars(sa.select(VideoSource.video_id).where(VideoSource.source_id == 1))
        )
        assert linked == {"new", "undated", "past_live", "done", "gave_up", "retry", "queued"}
        assert await due_sources(session, datetime.now(UTC)) == [2]

    # A second poll of the same listing finds every video queued or done.
    async with sessionmaker() as session:
        await session.execute(sa.update(Source).values(last_polled_at=None))
        await session.commit()
    await poll_source(
        sessionmaker,
        1,
        settings,
        lister=lambda url, c: TAB_LISTING,
        cancel=_cancel,
        prober=_no_probe,
    )
    async with sessionmaker() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Job)) == 9

    playlist = {
        "channel_id": "UC1",
        "channel": "Chan",
        "entries": [
            {"id": "other", "title": "Other", "channel_id": "UC2", "channel": "Two"},
            {"id": "private", "title": "[Private video]"},
        ],
    }
    await poll_source(
        sessionmaker,
        2,
        settings,
        lister=lambda url, c: playlist,
        cancel=_cancel,
        prober=_no_probe,
    )
    async with sessionmaker() as session:
        assert (await session.get(Channel, "UC2")).dir_name == "Two [UC2]"
        # A playlist source takes its channel from the listing.
        assert (await session.get(Source, 2)).channel_id == "UC1"
        assert await session.get(Video, "private") is None
        job = await session.scalar(sa.select(Job).where(Job.video_id == "other"))
        assert job.state is JobState.QUEUED

    def fails(url, cookiefile):
        raise RuntimeError("HTTP Error 429")

    await poll_source(sessionmaker, 2, settings, lister=fails, cancel=_cancel, prober=_no_probe)
    async with sessionmaker() as session:
        assert (await session.get(Source, 2)).last_error == "RuntimeError: HTTP Error 429"
    await engine.dispose()


def test_api_writes_sources(settings):
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    with TestClient(app, headers={"X-Requested-With": "savetube"}) as client:
        body = {"url": "https://www.youtube.com/@chan", "tabs": ["videos", "shorts"]}
        created = client.post("/api/sources", json=body)
        assert created.status_code == 201
        assert [s["url"] for s in created.json()] == [
            "https://www.youtube.com/@chan/videos",
            "https://www.youtube.com/@chan/shorts",
        ]
        assert client.post("/api/sources", json=body | {"tabs": ["shorts"]}).status_code == 409
        assert client.post("/api/sources", json=body | {"tabs": []}).status_code == 422

        source_id = created.json()[0]["id"]
        patched = client.patch(f"/api/sources/{source_id}", json={"enabled": False})
        assert patched.json()["enabled"] is False
        assert patched.json()["poll_interval_minutes"] == 360
        assert client.post(f"/api/sources/{source_id}/poll").status_code == 202

        for s in created.json():
            assert client.delete(f"/api/sources/{s['id']}").status_code == 204
        assert client.get("/api/sources").json() == []


def test_api_refuses_a_write_without_the_csrf_header(settings):
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    with TestClient(app) as client:
        response = client.post(
            "/api/sources", json={"url": "https://www.youtube.com/@chan", "tabs": ["videos"]}
        )
        assert response.status_code == 403
        assert client.get("/api/sources").json() == []


NASA = "UCLA_DiR1FfKNvjuUpBHmylQ"
UPCOMING = {
    "id": "pAKqJxuxDgk",
    "title": "Artemis Acceleration (Oct. 23, 2026)",
    "channel_id": NASA,
    "channel": "NASA",
    "live_status": "is_upcoming",
    "release_timestamp": 1792778400,
}
LIVE_NOW = {
    "id": "M3HKLzjvKPc",
    "title": "Live Video from the International Space Station (Official NASA Stream)",
    "channel_id": NASA,
    "channel": "NASA",
    "live_status": "is_live",
    "release_timestamp": 1785417281,
}


def _feed(*ids):
    entries = "".join(f"<entry><yt:videoId>{i}</yt:videoId></entry>" for i in ids)
    return (
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"'
        f' xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    )


def _not_live(channel_id):
    from yt_dlp.utils import DownloadError

    raise DownloadError("ERROR: [youtube:tab] @NASAGoddard: The channel is not currently live")


def _no_streams(channel_id):
    return {"entries": []}


def _watch(settings, channel_id=NASA):
    """A record job needs a channel watching lives and an enabled tab source of it."""
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name, watch_lives) VALUES"
        f" ('{channel_id}', 'NASA', 'NASA [{channel_id}]', true)",
        "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        f" ('https://www.youtube.com/channel/{channel_id}/streams', 'tab', '{channel_id}',"
        "  true, 360)",
    )


async def _record_jobs(sessionmaker):
    async with sessionmaker() as session:
        return list(await session.scalars(sa.select(Job).where(Job.kind == JobKind.RECORD)))


@pytest.mark.anyio
async def test_upcoming_stream_in_feed_gets_one_record_job_at_its_start(settings):
    from savetube.live import check_channel

    _watch(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    probes = {UPCOMING["id"]: UPCOMING}
    for _ in range(2):
        await check_channel(
            sessionmaker,
            NASA,
            fetch_feed=lambda c: _feed(UPCOMING["id"]),
            probe=probes.__getitem__,
            probe_live=_not_live,
            list_streams=_no_streams,
            seen={},
        )
    jobs = await _record_jobs(sessionmaker)
    assert [(j.video_id, j.state) for j in jobs] == [("pAKqJxuxDgk", JobState.QUEUED)]
    assert jobs[0].scheduled_at == datetime(2026, 10, 23, 18, 0, tzinfo=UTC)
    await engine.dispose()


@pytest.mark.anyio
async def test_stream_found_live_gets_a_record_job_without_a_start(settings):
    from savetube.live import check_channel

    _watch(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    created = await check_channel(
        sessionmaker,
        NASA,
        fetch_feed=lambda c: _feed(),
        probe=lambda v: pytest.fail("nothing in the feed to probe"),
        probe_live=lambda c: LIVE_NOW,
        list_streams=_no_streams,
        seen={},
    )
    jobs = await _record_jobs(sessionmaker)
    assert [(j.video_id, j.state, j.scheduled_at) for j in jobs] == [
        ("M3HKLzjvKPc", JobState.QUEUED, None)
    ]
    assert created == [jobs[0].id]
    await engine.dispose()


@pytest.mark.anyio
async def test_uploads_and_finished_streams_get_no_record_job(settings):
    from savetube.live import check_channel

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    probes = {
        "upload": {**UPCOMING, "id": "upload", "live_status": "not_live"},
        "ended": {**UPCOMING, "id": "ended", "live_status": "was_live"},
    }
    created = await check_channel(
        sessionmaker,
        NASA,
        fetch_feed=lambda c: _feed("upload", "ended"),
        probe=probes.__getitem__,
        probe_live=_not_live,
        list_streams=_no_streams,
        seen={},
    )
    assert created == []
    assert await _record_jobs(sessionmaker) == []
    await engine.dispose()


@pytest.mark.anyio
async def test_a_probed_id_is_not_probed_again(settings):
    from savetube.live import check_channel

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    calls = []

    def probe(video_id):
        calls.append(video_id)
        return {**UPCOMING, "id": video_id, "live_status": "not_live"}

    seen = {}
    for _ in range(2):
        await check_channel(
            sessionmaker,
            NASA,
            fetch_feed=lambda c: _feed("a", "b"),
            probe=probe,
            probe_live=_not_live,
            list_streams=_no_streams,
            seen=seen,
        )
    assert sorted(calls) == ["a", "b"]
    await engine.dispose()


@pytest.mark.anyio
async def test_watched_channels_watch_lives_and_have_an_enabled_tab(settings):
    from savetube.live import watched_channels

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name, watch_lives) VALUES"
        " ('UC1', 'a', 'a', true), ('UC2', 'b', 'b', false), ('UC3', 'c', 'c', true)",
        "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes)"
        " VALUES ('https://www.youtube.com/@a/streams', 'tab', 'UC1', true, 360),"
        "        ('https://www.youtube.com/@a/videos', 'tab', 'UC1', true, 360),"
        "        ('https://www.youtube.com/@b/streams', 'tab', 'UC2', true, 360),"
        "        ('https://www.youtube.com/@c/streams', 'tab', 'UC3', false, 360),"
        "        ('https://www.youtube.com/playlist?list=PL1', 'playlist', NULL, true, 360)",
    )
    engine = make_engine(settings.sqlalchemy_url)
    async with make_sessionmaker(engine)() as session:
        assert await watched_channels(session) == ["UC1"]
    await engine.dispose()


@pytest.mark.anyio
async def test_a_detected_live_is_archived_once_it_ends(settings):
    """Until a recorder exists, the VOD is the only copy of a detected live."""
    from savetube.poll import poll_source

    _sql(
        settings,
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@VESPERBELL/streams', 'tab', true, 360)",
        "INSERT INTO channel (id, name, dir_name) VALUES"
        " ('UCPd0Z22gF43dUidPYEQO5-w', 'VESPERBELL', 'VESPERBELL [UCPd0Z22gF43dUidPYEQO5-w]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('B_eFxgIWdXo', 'UCPd0Z22gF43dUidPYEQO5-w', 'lives', 't', 'pending')",
        "INSERT INTO job (kind, state, video_id, attempts) VALUES"
        " ('record', 'queued', 'B_eFxgIWdXo', 0)",
    )
    listing = {
        "channel_id": "UCPd0Z22gF43dUidPYEQO5-w",
        "channel": "VESPERBELL",
        "entries": [
            {
                "id": "B_eFxgIWdXo",
                "title": "【歌枠】ちょっと疲れちゃってるんだと思うよ【VESPERBELL ヨミ】",
                "live_status": "was_live",
            }
        ],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    await poll_source(
        sessionmaker, 1, settings, lister=lambda u, c: listing, cancel=_cancel, prober=_no_probe
    )
    async with sessionmaker() as session:
        kinds = sorted(
            await session.scalars(sa.select(Job.kind).where(Job.video_id == "B_eFxgIWdXo"))
        )
    assert kinds == [JobKind.ARCHIVE, JobKind.RECORD]
    await engine.dispose()


def test_api_lists_a_recording_with_its_scheduled_start(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'a', 'a')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('pAKqJxuxDgk', 'UC1', 'lives', 't', 'pending')",
        "INSERT INTO job (kind, state, video_id, attempts, scheduled_at) VALUES"
        " ('record', 'queued', 'pAKqJxuxDgk', 0, '2026-10-23T18:00:00Z')",
    )
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    with TestClient(app) as client:
        [job] = client.get("/api/jobs", params={"kind": "record"}).json()
    assert job.get("scheduled_at") == "2026-10-23T18:00:00Z"


@pytest.mark.anyio
async def test_one_unreadable_feed_video_does_not_stop_detection(settings):
    from yt_dlp.utils import DownloadError

    from savetube.live import check_channel

    _watch(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    def probe(video_id):
        if video_id == "members":
            raise DownloadError("ERROR: [youtube] members: Join this channel to get access")
        return UPCOMING

    await check_channel(
        sessionmaker,
        NASA,
        fetch_feed=lambda c: _feed("members", UPCOMING["id"]),
        probe=probe,
        probe_live=lambda c: LIVE_NOW,
        list_streams=_no_streams,
        seen={},
    )
    assert sorted(j.video_id for j in await _record_jobs(sessionmaker)) == [
        "M3HKLzjvKPc",
        "pAKqJxuxDgk",
    ]
    await engine.dispose()


@pytest.mark.anyio
async def test_a_members_only_live_is_probed_again_with_a_member_profile(settings):
    from yt_dlp.utils import DownloadError

    from savetube.live import check_channel

    _watch(settings)
    _sql(
        settings,
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        f" ('{NASA}', 'alt', false, now(), 'm0'), ('{NASA}', 'main', true, now(), 'm0')",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    members_live = UPCOMING | {"id": "memlive", "title": "Members stream"}
    retried = []

    def probe(video_id):
        if video_id == "memlive":
            raise DownloadError(MEMBERS_ONLY_ERROR)
        if video_id == "private":
            raise DownloadError("ERROR: [youtube] private: Private video")
        return LIVE_NOW

    def member_probe(video_id, profile):
        retried.append((video_id, profile))
        return members_live

    async def check(profiles):
        await check_channel(
            sessionmaker,
            NASA,
            fetch_feed=lambda c: _feed("memlive", "private", LIVE_NOW["id"]),
            probe=probe,
            probe_live=_not_live,
            list_streams=_no_streams,
            seen={},
            member_probe=member_probe,
            profiles=profiles,
        )

    # The member profile is not present: no second probe, as before.
    await check(["alt"])
    assert retried == []
    assert [j.video_id for j in await _record_jobs(sessionmaker)] == [LIVE_NOW["id"]]

    # Only the members-only refusal is probed again, with the member profile.
    await check(["alt", "main"])
    assert retried == [("memlive", "main")]
    jobs = {j.video_id: j for j in await _record_jobs(sessionmaker)}
    assert sorted(jobs) == [LIVE_NOW["id"], "memlive"]
    async with sessionmaker() as session:
        video = await session.get(Video, "memlive")
    # Its recording takes a member profile (savetube.jobs).
    assert video.type is VideoType.MEMBERS
    await engine.dispose()


@pytest.mark.anyio
async def test_upcoming_stream_pushed_out_of_the_feed_is_found_on_the_live_tab(settings):
    """NASA's feed held 15 newer uploads, and pAKqJxuxDgk was only on the Live tab."""
    from savetube.live import check_channel

    _watch(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    streams = {
        "channel_id": NASA,
        "entries": [
            {"id": "pAKqJxuxDgk", "live_status": "is_upcoming"},
            {"id": "past", "live_status": "was_live"},
        ],
    }
    await check_channel(
        sessionmaker,
        NASA,
        fetch_feed=lambda c: _feed(),
        probe={UPCOMING["id"]: UPCOMING}.__getitem__,
        probe_live=_not_live,
        list_streams=lambda c: streams,
        seen={},
    )
    jobs = await _record_jobs(sessionmaker)
    assert [(j.video_id, j.scheduled_at) for j in jobs] == [
        ("pAKqJxuxDgk", datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
    ]
    await engine.dispose()


VESP = "UCPd0Z22gF43dUidPYEQO5-w"
UTAWAKU = "【歌枠】ちょっと疲れちゃってるんだと思うよ【VESPERBELL ヨミ】"
GUERRILLA = "【ゲリラ(豪雨)】私魔女のキキ(危機)、こっちは配信すっぽかし女の【VESPERBELL カスカ】"
# Trimmed from `yt-dlp --flat-playlist -J` on VESPERBELL's /playlists tab, 2026-10.
PLAYLISTS_LISTING = {
    "id": VESP,
    "channel": "VESPERBELL",
    "channel_id": VESP,
    "uploader_id": "@VESPERBELL",
    "entries": [
        {
            "_type": "url",
            "ie_key": "YoutubeTab",
            "id": f"PLu4d2yplfpFd{n}",
            "url": f"https://www.youtube.com/playlist?list=PLu4d2yplfpFd{n}",
            "title": title,
            "channel": "Playlist",
            "channel_id": VESP,
        }
        for n, title in (("A", "◀︎YOMI Stream▶︎"), ("B", "◀︎KASUKA Stream▶︎"), ("C", "◀︎BD LIVE▶︎"))
    ],
}


def _api(settings, lister=None):
    from savetube.web.api import get_scanner

    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    if lister is not None:
        app.dependency_overrides[get_scanner] = lambda: lister
    return TestClient(app, headers={"X-Requested-With": "savetube"})


async def _cancel(queue_job_id):
    return True


def _no_probe(video_id, cookiefile):
    pytest.fail("no membership check expected")


async def _archive_jobs(sessionmaker):
    async with sessionmaker() as session:
        return sorted(
            await session.scalars(sa.select(Job.video_id).where(Job.kind == JobKind.ARCHIVE))
        )


@pytest.mark.anyio
async def test_poll_keeps_the_full_listing_and_archives_titles_the_words_pass(settings):
    from savetube.poll import poll_source

    _sql(
        settings,
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes,"
        " include_words, exclude_words) VALUES"
        " (1, 'https://www.youtube.com/@VESPERBELL/streams', 'tab', true, 360,"
        "  '{歌枠}', '{メン限}')",
    )
    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [
            {"id": "B_eFxgIWdXo", "title": UTAWAKU, "live_status": "was_live"},
            {"id": "guerrilla", "title": GUERRILLA, "live_status": "was_live"},
            {"id": "members", "title": "【歌枠】メン限", "live_status": "was_live"},
            {"id": "soon", "title": "【歌枠】予定", "live_status": "is_upcoming"},
        ],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await poll_source(
        sessionmaker, 1, settings, lister=lambda u, c: listing, cancel=_cancel, prober=_no_probe
    )
    assert await _archive_jobs(sessionmaker) == ["B_eFxgIWdXo"]

    with _api(settings) as client:
        preview = client.post(
            "/api/sources/1/title-preview", json={"include_words": [], "exclude_words": []}
        ).json()
        # Every listed title, the ones the words skip and the upcoming one too.
        assert preview["total"] == 4 and preview["kept"] == 4
        assert {t["video_id"] for t in preview["titles"]} == {
            "B_eFxgIWdXo",
            "guerrilla",
            "members",
            "soon",
        }
        preview = client.post(
            "/api/sources/1/title-preview",
            json={"include_words": [" 歌枠 ", ""], "exclude_words": ["メン限"]},
        ).json()
        assert (preview["total"], preview["kept"]) == (4, 2)
        assert {t["video_id"]: t["kept"] for t in preview["titles"]} == {
            "B_eFxgIWdXo": True,
            "guerrilla": False,
            "members": False,
            "soon": True,
        }
        words = client.get(f"/api/channels/{VESP}/words").json()
        assert words[0] == {"word": "歌枠", "count": 3}
        assert {"word": "VESPERBELL ヨミ", "count": 1} not in words
        assert client.get("/api/channels/UCnope/words").status_code == 404

    # The next poll replaces the rows: a video gone from the listing leaves them.
    listing["entries"] = listing["entries"][:1]
    async with sessionmaker() as session:
        await session.execute(sa.update(Source).values(last_polled_at=None))
        await session.commit()
    await poll_source(
        sessionmaker, 1, settings, lister=lambda u, c: listing, cancel=_cancel, prober=_no_probe
    )
    with _api(settings) as client:
        preview = client.post(
            "/api/sources/1/title-preview", json={"include_words": [], "exclude_words": []}
        ).json()
    assert preview == {
        "total": 1,
        "kept": 1,
        "titles": [{"video_id": "B_eFxgIWdXo", "title": UTAWAKU, "kept": True}],
    }
    await engine.dispose()


@pytest.mark.anyio
async def test_playlists_source_makes_a_child_per_new_playlist_and_archives_nothing(settings):
    from savetube.poll import poll_source

    _sql(
        settings,
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes,"
        " include_words, exclude_words, other_channels) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/playlists', 'playlists', true, 120,"
        "  '{歌枠}', '{告知}', true),"
        " (2, 'https://www.youtube.com/playlist?list=PLu4d2yplfpFdB', 'playlist', true, 360,"
        "  '{}', '{}', false)",
        # Explicit ids leave the sequence behind.
        "SELECT setval('source_id_seq', 2)",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    for _ in range(2):
        async with sessionmaker() as session:
            await session.execute(sa.update(Source).values(last_polled_at=None))
            await session.commit()
        await poll_source(
            sessionmaker,
            1,
            settings,
            lister=lambda u, c: PLAYLISTS_LISTING,
            cancel=_cancel,
            prober=_no_probe,
        )
    assert await _archive_jobs(sessionmaker) == []

    async with sessionmaker() as session:
        parent = await session.get(Source, 1)
        assert parent.last_error is None and parent.channel_id == VESP
        children = list(
            await session.scalars(sa.select(Source).where(Source.id > 2).order_by(Source.url))
        )
        assert await session.scalar(sa.select(sa.func.count()).select_from(Video)) == 0
    assert [
        (
            c.url,
            c.kind,
            c.parent_id,
            c.channel_id,
            c.poll_interval_minutes,
            c.include_words,
            c.exclude_words,
            c.other_channels,
        )
        for c in children
    ] == [
        (
            f"https://www.youtube.com/playlist?list=PLu4d2yplfpFd{n}",
            SourceKind.PLAYLIST,
            1,
            VESP,
            120,
            # Words are not copied. A child merges its parent's lists when matched.
            [],
            [],
            True,
        )
        for n in ("A", "C")
    ]

    with _api(settings) as client:
        assert client.delete("/api/sources/1").status_code == 204
        assert [s["id"] for s in client.get("/api/sources").json()] == [2]
    await engine.dispose()


@pytest.mark.anyio
async def test_playlist_source_skips_other_channels_unless_allowed(settings):
    from savetube.poll import poll_source

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes)"
        f" VALUES (1, 'https://www.youtube.com/channel/{VESP}/playlists', 'playlists', '{VESP}',"
        "  false, 360)",
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes, parent_id,"
        " other_channels) VALUES"
        " (2, 'https://www.youtube.com/playlist?list=PLown', 'playlist', true, 360, 1, false),"
        " (3, 'https://www.youtube.com/playlist?list=PLall', 'playlist', true, 360, 1, true)",
    )
    # A playlist listing whose top level names no channel: the parent's applies.
    listing = {
        "entries": [
            {"id": "own", "title": UTAWAKU, "channel_id": VESP, "live_status": "was_live"},
            {
                "id": "EoPQm9SemWw",
                "title": "【歌枠】じゃ、歌うね・・・・  #ヨミネロ #RKMFes_2026",
                "channel_id": "UCwuS0uY-Z2Gr_5OV2oFybFA",
                "channel": "KMNZ",
                "live_status": "was_live",
            },
        ]
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await poll_source(
        sessionmaker, 2, settings, lister=lambda u, c: listing, cancel=_cancel, prober=_no_probe
    )
    assert await _archive_jobs(sessionmaker) == ["own"]
    async with sessionmaker() as session:
        assert (await session.get(Source, 2)).channel_id == VESP

    await poll_source(
        sessionmaker, 3, settings, lister=lambda u, c: listing, cancel=_cancel, prober=_no_probe
    )
    assert await _archive_jobs(sessionmaker) == ["EoPQm9SemWw", "own"]
    await engine.dispose()


def test_api_sources_carry_title_words_and_list_channel_playlists(settings):
    listed = []

    def lister(url, limit):
        listed.append(url)
        return PLAYLISTS_LISTING

    with _api(settings, lister) as client:
        created = client.post(
            "/api/sources",
            json={
                "url": f"https://www.youtube.com/channel/{VESP}",
                "tabs": ["streams", "playlists"],
                "include_words": [" 歌枠", "歌枠 ", "", "Cover", "cover"],
                "other_channels": True,
            },
        )
        assert created.status_code == 201
        assert [
            (s["url"], s["kind"], s["include_words"], s["exclude_words"], s["other_channels"])
            for s in created.json()
        ] == [
            (f"https://www.youtube.com/channel/{VESP}/streams", "tab", ["歌枠", "Cover"], [], True),
            (
                f"https://www.youtube.com/channel/{VESP}/playlists",
                "playlists",
                ["歌枠", "Cover"],
                [],
                True,
            ),
        ]
        streams_id = created.json()[0]["id"]
        patched = client.patch(
            f"/api/sources/{streams_id}",
            json={"exclude_words": ["告知", " 告知"], "other_channels": False},
        ).json()
        assert (patched["include_words"], patched["exclude_words"]) == (["歌枠", "Cover"], ["告知"])
        assert patched["other_channels"] is False
        assert client.get("/api/sources").json()[0]["parent_id"] is None

        single = client.post(
            "/api/sources",
            json={"url": "https://www.youtube.com/playlist?list=PLu4d2yplfpFdB"},
        ).json()[0]
        assert single["include_words"] == [] and single["other_channels"] is False

        playlists = client.post(
            "/api/channel-playlists", json={"url": f"https://www.youtube.com/channel/{VESP}"}
        )
        assert playlists.status_code == 200
        assert listed == [f"https://www.youtube.com/channel/{VESP}/playlists"]
        assert playlists.json() == [
            {
                "id": f"PLu4d2yplfpFd{n}",
                "title": title,
                "url": f"https://www.youtube.com/playlist?list=PLu4d2yplfpFd{n}",
                "source_id": single["id"] if n == "B" else None,
            }
            for n, title in (("A", "◀︎YOMI Stream▶︎"), ("B", "◀︎KASUKA Stream▶︎"), ("C", "◀︎BD LIVE▶︎"))
        ]
        bad = client.post("/api/channel-playlists", json={"url": "https://evil.example/@x"})
        assert bad.status_code == 422
        assert listed == [f"https://www.youtube.com/channel/{VESP}/playlists"]


@pytest.mark.anyio
async def test_a_live_is_recorded_only_when_a_watching_source_passes_its_title(settings):
    from savetube.live import check_channel

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name, watch_lives) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]', true)",
        "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes,"
        " include_words, exclude_words) VALUES"
        f" ('https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360,"
        "  '{歌枠}', '{メン限}'),"
        f" ('https://www.youtube.com/channel/{VESP}/videos', 'tab', '{VESP}', true, 360,"
        "  '{3DLIVE}', '{}'),"
        # Empty words pass every title, but these two never watch: one is
        # disabled, the other is not a tab.
        f" ('https://www.youtube.com/channel/{VESP}/shorts', 'tab', '{VESP}', false, 360,"
        "  '{}', '{}'),"
        f" ('https://www.youtube.com/playlist?list=PLx', 'playlist', '{VESP}', true, 360,"
        "  '{}', '{}')",
    )

    def stream(video_id, title, status="is_upcoming"):
        return {
            "id": video_id,
            "title": title,
            "channel_id": VESP,
            "channel": "VESPERBELL",
            "live_status": status,
            "release_timestamp": 1792778400,
        }

    probes = {
        "utawaku": stream("utawaku", UTAWAKU),
        "guerrilla": stream("guerrilla", GUERRILLA),
        "members": stream("members", "【歌枠】メン限"),
    }
    probed = []

    def probe(video_id):
        probed.append(video_id)
        return probes[video_id]

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    seen = {}
    for _ in range(2):
        await check_channel(
            sessionmaker,
            VESP,
            fetch_feed=lambda c: _feed("utawaku", "guerrilla", "members"),
            probe=probe,
            probe_live=lambda c: stream(
                "3d", "【3DLIVE】 VESPERBELL カスカ Birthday Live 2026", "is_live"
            ),
            list_streams=_no_streams,
            seen=seen,
        )
    # The watching /streams tab decides alone, so the /videos tab's 3DLIVE
    # word does not record "3d".
    assert sorted(j.video_id for j in await _record_jobs(sessionmaker)) == ["utawaku"]
    # A skipped detection is not probed again by the same process.
    assert sorted(probed) == ["guerrilla", "members", "utawaku"]
    await engine.dispose()


@pytest.mark.anyio
async def test_poll_and_preview_merge_global_channel_and_source_words(settings):
    from savetube.poll import poll_source

    _sql(
        settings,
        "UPDATE global_words SET include_words = '{歌枠}', exclude_words = '{告知}'",
        "INSERT INTO channel (id, name, dir_name, include_words) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]', '{{cover}}')",
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes,"
        " exclude_words) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', true, 360,"
        "  '{メン限}')",
    )
    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [
            {"id": "utawaku", "title": UTAWAKU, "live_status": "was_live"},
            {"id": "cover", "title": "IRIS OUT - 米津玄師 (Cover) / VESPERBELL ヨミ"},
            {"id": "guerrilla", "title": GUERRILLA, "live_status": "was_live"},
            {"id": "members", "title": "【歌枠】メン限", "live_status": "was_live"},
            {"id": "notice", "title": "【歌枠】重大告知アリ", "live_status": "was_live"},
        ],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await poll_source(
        sessionmaker, 1, settings, lister=lambda u, c: listing, cancel=_cancel, prober=_no_probe
    )
    assert await _archive_jobs(sessionmaker) == ["cover", "utawaku"]

    with _api(settings) as client:
        # The body replaces the source's own lists. The stored global and
        # channel lists still apply.
        preview = client.post(
            "/api/sources/1/title-preview", json={"include_words": [], "exclude_words": []}
        ).json()
        assert {t["video_id"]: t["kept"] for t in preview["titles"]} == {
            "utawaku": True,
            "cover": True,
            "guerrilla": False,
            "members": True,
            "notice": False,
        }
        assert (preview["total"], preview["kept"]) == (5, 3)
        preview = client.post(
            "/api/sources/1/title-preview",
            json={"include_words": ["ゲリラ"], "exclude_words": ["ヨミ"]},
        ).json()
        assert {t["video_id"] for t in preview["titles"] if t["kept"]} == {
            "guerrilla",
            "members",
        }
    await engine.dispose()


@pytest.mark.anyio
async def test_a_global_exclude_word_stops_a_live_detection(settings):
    from savetube.live import check_channel

    _watch(settings)
    _sql(
        settings,
        "UPDATE global_words SET exclude_words = '{Artemis}'",
        f"UPDATE channel SET include_words = '{{Space Station}}' WHERE id = '{NASA}'",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await check_channel(
        sessionmaker,
        NASA,
        fetch_feed=lambda c: _feed(UPCOMING["id"]),
        probe={UPCOMING["id"]: UPCOMING}.__getitem__,
        probe_live=lambda c: LIVE_NOW,
        list_streams=_no_streams,
        seen={},
    )
    assert [j.video_id for j in await _record_jobs(sessionmaker)] == [LIVE_NOW["id"]]
    await engine.dispose()


def test_api_global_and_channel_words(settings):
    _sql(settings, "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')")
    with _api(settings) as client:
        assert client.get("/api/words").json() == {"include_words": [], "exclude_words": []}
        put = client.put(
            "/api/words", json={"include_words": [" 歌枠", "歌枠", ""], "exclude_words": ["告知"]}
        )
        assert put.status_code == 200
        assert put.json() == {"include_words": ["歌枠"], "exclude_words": ["告知"]}
        assert client.get("/api/words").json() == put.json()

        [channel] = client.get("/api/channels").json()
        assert (channel["include_words"], channel["exclude_words"]) == ([], [])
        patched = client.patch("/api/channels/UC1", json={"include_words": ["Cover", "cover "]})
        assert patched.status_code == 200
        assert patched.json()["include_words"] == ["Cover"]
        assert patched.json()["exclude_words"] == []
        assert patched.json()["video_count"] == 0
        patched = client.patch("/api/channels/UC1", json={"exclude_words": ["Shorts"]})
        assert (patched.json()["include_words"], patched.json()["exclude_words"]) == (
            ["Cover"],
            ["Shorts"],
        )
        assert client.get("/api/channels").json()[0]["exclude_words"] == ["Shorts"]
        assert client.patch("/api/channels/UCnope", json={}).status_code == 404


def test_api_channel_playlists_reports_a_yt_dlp_failure_as_502(settings):
    from yt_dlp.utils import DownloadError

    def lister(url, limit):
        raise DownloadError("ERROR: [youtube:tab] @gone: This channel does not exist.")

    with _api(settings, lister) as client:
        response = client.post(
            "/api/channel-playlists", json={"url": "https://www.youtube.com/@gone"}
        )
    assert response.status_code == 502
    assert "This channel does not exist." in response.json()["detail"]


KMNZ = "UCwuS0uY-Z2Gr_5OV2oFybFA"
PL = "https://www.youtube.com/playlist?list=PLu4d2yplfpFd"


def _parent(settings, *extra, enabled="true", other_channels="false"):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " include_words, other_channels) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/playlists', 'playlists', '{VESP}',"
        f"  {enabled}, 120, '{{歌枠}}', {other_channels})",
        "SELECT setval('source_id_seq', 1)",
        *extra,
    )


async def _poll(sessionmaker, settings, source_id, listing, cancel=_cancel, prober=_no_probe):
    from savetube.poll import poll_source

    await poll_source(
        sessionmaker,
        source_id,
        settings,
        lister=lambda u, c: listing,
        cancel=cancel,
        prober=prober,
    )


async def _children(sessionmaker):
    async with sessionmaker() as session:
        return list(
            await session.scalars(
                sa.select(Source).where(Source.parent_id == 1).order_by(Source.url)
            )
        )


@pytest.mark.anyio
async def test_a_child_is_disabled_not_deleted_and_never_recreated(settings):
    _parent(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    a = (await _children(sessionmaker))[0]
    assert a.url == f"{PL}A"

    with _api(settings) as client:
        refused = client.delete(f"/api/sources/{a.id}")
        assert refused.status_code == 409
        assert refused.json()["detail"] == "a playlist of a tracked channel can only be disabled"
        assert client.patch(f"/api/sources/{a.id}", json={"enabled": False}).status_code == 200
        assert "ignored_urls" not in client.get("/api/sources").json()[0]
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    children = await _children(sessionmaker)
    assert [(c.id, c.url, c.enabled, c.title) for c in children] == [
        (a.id, f"{PL}A", False, "◀︎YOMI Stream▶︎"),
        (children[1].id, f"{PL}B", True, "◀︎KASUKA Stream▶︎"),
        (children[2].id, f"{PL}C", True, "◀︎BD LIVE▶︎"),
    ]

    with _api(settings) as client:
        restored = client.patch(f"/api/sources/{a.id}", json={"enabled": True}).json()
    assert restored["enabled"] is True
    await engine.dispose()


@pytest.mark.anyio
async def test_new_children_spread_their_first_poll_over_the_interval(settings):
    from savetube.poll import due_sources

    _parent(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    before = datetime.now(UTC)
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    after = datetime.now(UTC)
    children = await _children(sessionmaker)
    assert len(children) == 3
    for c in children:
        assert before - timedelta(minutes=120) <= c.last_polled_at <= after
    async with sessionmaker() as session:
        assert await due_sources(session, after) == []
    await engine.dispose()


@pytest.mark.anyio
async def test_a_disabled_parent_holds_its_children(settings):
    from savetube.poll import due_sources

    _parent(
        settings,
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes, parent_id)"
        f" VALUES (2, '{PL}A', 'playlist', true, 120, 1),"
        "        (3, 'https://www.youtube.com/playlist?list=PLlone', 'playlist', true, 120,"
        "         NULL)",
        enabled="false",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        assert await due_sources(session, datetime.now(UTC)) == [3]
        await session.execute(sa.update(Source).where(Source.id == 1).values(enabled=True))
        await session.commit()
        assert await due_sources(session, datetime.now(UTC)) == [1, 2, 3]
    await engine.dispose()


def test_patching_a_parent_copies_its_settings_but_not_its_words(settings):
    _parent(
        settings,
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes, parent_id,"
        " include_words) VALUES (2, '{PL}A', 'playlist', true, 120, 1, '{Cover}')".replace(
            "{PL}", PL
        ),
    )
    with _api(settings) as client:
        response = client.patch(
            "/api/sources/1",
            json={
                "poll_interval_minutes": 60,
                "other_channels": True,
                "include_words": ["告知"],
                "enabled": False,
            },
        )
        assert response.status_code == 200
        child = next(s for s in client.get("/api/sources").json() if s["id"] == 2)
    assert (
        child["poll_interval_minutes"],
        child["other_channels"],
        child["include_words"],
        child["enabled"],
    ) == (60, True, ["Cover"], True)


@pytest.mark.anyio
async def test_a_child_merges_parent_words_and_each_uploaders_channel_words(settings):
    _parent(
        settings,
        f"UPDATE channel SET exclude_words = '{{告知}}' WHERE id = '{VESP}'",
        "INSERT INTO channel (id, name, dir_name, exclude_words) VALUES"
        f" ('{KMNZ}', 'KMNZ', 'KMNZ [{KMNZ}]', '{{ヨミネロ}}')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        f" parent_id, other_channels) VALUES (2, '{PL}A', 'playlist', '{VESP}', true, 120, 1,"
        "  true)",
    )

    def e(video_id, title, channel_id=VESP):
        return {"id": video_id, "title": title, "channel_id": channel_id, "live_status": "was_live"}

    listing = {
        # The listing never overrides a child's channel.
        "channel_id": "UCsomeoneelse",
        "channel": "Someone",
        "entries": [
            e("own", "【歌枠】A"),
            e("own_notice", "【歌枠】重大告知"),
            e("kmnz_tag", "【歌枠】じゃ、歌うね #ヨミネロ", KMNZ),
            e("kmnz_notice", "【歌枠】告知", KMNZ),
            e("chat", "【雑談】B"),
        ],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 2, listing)
    assert await _archive_jobs(sessionmaker) == ["kmnz_notice", "own"]
    async with sessionmaker() as session:
        child = await session.get(Source, 2)
        assert child.channel_id == VESP and child.last_error is None

    with _api(settings) as client:
        preview = client.post(
            "/api/sources/2/title-preview", json={"include_words": [], "exclude_words": []}
        ).json()
    assert {t["video_id"] for t in preview["titles"] if t["kept"]} == {"own", "kmnz_notice"}
    await engine.dispose()


@pytest.mark.anyio
async def test_a_playlist_without_an_owner_needs_other_channels(settings):
    _sql(
        settings,
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/playlist?list=PLmix', 'playlist', true, 360)",
    )
    listing = {
        "entries": [
            {"id": "a", "title": "A", "channel_id": VESP, "channel": "V", "live_status": "was_live"}
        ]
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, listing)
    assert await _archive_jobs(sessionmaker) == []
    async with sessionmaker() as session:
        assert (await session.get(Source, 1)).last_error == (
            "playlist has no owner channel; turn on other channels to archive it"
        )
    await engine.dispose()


@pytest.mark.anyio
async def test_poll_links_every_stored_video_and_cancels_queued_jobs_the_words_drop(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " exclude_words) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360,"
        "  '{告知}'),"
        f" (2, 'https://www.youtube.com/channel/{VESP}/videos', 'tab', '{VESP}', true, 360,"
        "  '{}')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('stored', '{VESP}', 'lives', 's', 'complete'),"
        f" ('queued', '{VESP}', 'lives', 'q', 'pending'),"
        f" ('unqueued', '{VESP}', 'lives', 'u', 'pending'),"
        f" ('started', '{VESP}', 'lives', 'r', 'pending'),"
        f" ('running', '{VESP}', 'lives', 'r', 'downloading'),"
        f" ('shared', '{VESP}', 'lives', 'sh', 'pending')",
        "INSERT INTO video_source (video_id, source_id) VALUES ('shared', 2)",
        "INSERT INTO job (id, kind, state, video_id, attempts, queue_job_id) VALUES"
        " (101, 'archive', 'queued', 'queued', 0, 77),"
        " (102, 'archive', 'queued', 'unqueued', 0, NULL),"
        " (103, 'archive', 'queued', 'started', 0, 78),"
        " (104, 'archive', 'running', 'running', 1, 79),"
        " (105, 'archive', 'queued', 'shared', 0, 80)",
        "SELECT setval('job_id_seq', 200)",
    )

    def e(video_id, title):
        return {"id": video_id, "title": title, "live_status": "was_live"}

    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [
            e("stored", "告知 stored"),
            e("queued", "告知 queued"),
            e("unqueued", "告知 unqueued"),
            e("started", "告知 started"),
            e("running", "告知 running"),
            e("shared", "告知 shared"),
            e("skipped_new", "告知 new"),
            e("wanted", "歌枠"),
        ],
    }
    cancelled = []

    async def cancel(queue_job_id):
        cancelled.append(queue_job_id)
        # The worker took 78 before the cancel arrived.
        return queue_job_id != 78

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, listing, cancel)
    async with sessionmaker() as session:
        linked = set(
            await session.scalars(sa.select(VideoSource.video_id).where(VideoSource.source_id == 1))
        )
        states = dict(
            (await session.execute(sa.select(Job.id, Job.state).where(Job.id > 100))).all()
        )
        assert await session.get(Video, "skipped_new") is None
    assert linked == {"stored", "queued", "unqueued", "started", "running", "shared", "wanted"}
    assert sorted(cancelled) == [77, 78]
    assert states[101] is JobState.CANCELLED
    assert states[102] is JobState.CANCELLED
    assert states[103] is JobState.QUEUED
    assert states[104] is JobState.RUNNING
    # Another source lists `shared`, so this source's words do not cancel it.
    assert states[105] is JobState.QUEUED
    await engine.dispose()


@pytest.mark.anyio
async def test_a_queued_job_another_source_lists_is_cancelled_unless_an_archiving_source_wants_it(
    settings,
):
    tab = f"https://www.youtube.com/channel/{VESP}"
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, archiving,"
        " poll_interval_minutes, exclude_words) VALUES"
        f" (1, '{tab}/streams', 'tab', '{VESP}', true, true, 360, '{{告知}}'),"
        # Wants every title.
        f" (2, '{tab}/videos', 'tab', '{VESP}', true, true, 360, '{{}}'),"
        # Skips the title too.
        f" (3, '{PL}A', 'playlist', '{VESP}', true, true, 360, '{{告知}}'),"
        f" (4, '{PL}B', 'playlist', '{VESP}', true, false, 360, '{{}}'),"
        f" (5, '{PL}C', 'playlist', '{VESP}', false, true, 360, '{{}}')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('wanted', '{VESP}', 'lives', 'w', 'pending'),"
        f" ('also_skipped', '{VESP}', 'lives', 'a', 'pending'),"
        f" ('not_archiving', '{VESP}', 'lives', 'n', 'pending'),"
        f" ('disabled', '{VESP}', 'lives', 'd', 'pending')",
        "INSERT INTO video_source (video_id, source_id) VALUES"
        " ('wanted', 2), ('wanted', 3), ('also_skipped', 3), ('not_archiving', 4),"
        " ('disabled', 5)",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (101, 'archive', 'queued', 'wanted', 0),"
        " (102, 'archive', 'queued', 'also_skipped', 0),"
        " (103, 'archive', 'queued', 'not_archiving', 0),"
        " (104, 'archive', 'queued', 'disabled', 0)",
        "SELECT setval('job_id_seq', 200)",
    )
    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [
            {"id": v, "title": f"告知 {v}", "live_status": "was_live"}
            for v in ("wanted", "also_skipped", "not_archiving", "disabled")
        ],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, listing)
    async with sessionmaker() as session:
        states = dict(
            (await session.execute(sa.select(Job.id, Job.state).where(Job.id > 100))).all()
        )
    await engine.dispose()
    assert states == {
        101: JobState.QUEUED,
        102: JobState.CANCELLED,
        103: JobState.CANCELLED,
        104: JobState.CANCELLED,
    }


def _profiles(settings, *names):
    """Settings whose SAVETUBE_COOKIES_DIR holds a file per name, its content `# <name>`."""
    root = settings.archive_root.parent / "cookies"
    root.mkdir(exist_ok=True)
    for name in names:
        (root / f"{name}.txt").write_text(f"# {name}\n")
    # A Secret mount's own entries are no profiles.
    (root / "..data").mkdir(exist_ok=True)
    return settings.model_copy(update={"cookies_dir": root})


MEMBERS_ONLY_ERROR = (
    "ERROR: [youtube] member: Join this channel to get access to members-only content"
    " like this video, and other exclusive perks."
)
BOT_CHECK_ERROR = (
    "ERROR: [youtube] member: Sign in to confirm you’re not a bot."
    " Use --cookies-from-browser or --cookies for the authentication."
)


@pytest.mark.anyio
async def test_members_only_videos_wait_for_a_confirmed_member_profile(settings, caplog):
    import logging

    from yt_dlp.utils import DownloadError

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360)",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('queued_member', '{VESP}', 'members', 'q', 'pending')",
        "INSERT INTO video_source (video_id, source_id) VALUES ('queued_member', 1)",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (101, 'archive', 'queued', 'queued_member', 0)",
        "SELECT setval('job_id_seq', 200)",
    )
    settings = _profiles(settings, "main", "alt")

    def e(video_id, availability):
        return {
            "id": video_id,
            "title": video_id,
            "live_status": "was_live",
            "availability": availability,
        }

    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [
            e("public", "public"),
            e("member", "subscriber_only"),
            e("queued_member", "subscriber_only"),
        ],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    probed = []
    errors = {"alt": MEMBERS_ONLY_ERROR}

    def prober(video_id, cookiefile):
        profile = cookiefile.read_text().removeprefix("# ").strip()
        probed.append((video_id, profile))
        if profile in errors:
            raise DownloadError(errors[profile])
        return {"id": video_id, "formats": [{"format_id": "251"}]}

    async def queued():
        async with sessionmaker() as session:
            return sorted(
                await session.scalars(sa.select(Job.video_id).where(Job.state == JobState.QUEUED))
            )

    def memberships():
        engine = sa.create_engine(settings.sqlalchemy_url)
        with engine.connect() as c:
            rows = c.execute(
                sa.text(
                    "SELECT channel_id, profile, is_member, video_id, checked_at FROM membership"
                    " ORDER BY profile"
                )
            ).all()
        engine.dispose()
        return rows

    # No result yet: the poll skips the members-only videos, then checks
    # each profile with the first one it listed.
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _poll(sessionmaker, settings, 1, listing, prober=prober)
    assert await queued() == ["public"]
    async with sessionmaker() as session:
        assert (await session.get(Job, 101)).state is JobState.CANCELLED
        listed = set(await session.scalars(sa.select(ListingEntry.video_id)))
        linked = set(await session.scalars(sa.select(VideoSource.video_id)))
    assert listed == {"public", "member", "queued_member"}
    assert linked == {"public", "queued_member"}
    assert probed == [("member", "alt"), ("member", "main")]
    assert [tuple(r)[:4] for r in memberships()] == [
        (VESP, "alt", False, "member"),
        (VESP, "main", True, "member"),
    ]
    checked = [_fields(r) for r in caplog.records if r.getMessage() == "membership.checked"]
    assert [(f["channel"], f["profile"], f["member"]) for f in checked] == [
        ("VESPERBELL", "alt", "no"),
        ("VESPERBELL", "main", "yes"),
    ]

    # A confirmed member lets them pass. Fresh results are not checked again.
    await _poll(sessionmaker, settings, 1, listing, prober=prober)
    assert await queued() == ["member", "public", "queued_member"]
    assert len(probed) == 2

    # Stale results wait while a YouTube block pauses downloads.
    _sql(
        settings,
        "UPDATE membership SET checked_at = now() - interval '25 hours'",
        "UPDATE download_settings SET blocked_until = now() + interval '1 hour'",
    )
    await _poll(sessionmaker, settings, 1, listing, prober=prober)
    assert len(probed) == 2

    # A bot check keeps the previous result and stops the checks after it.
    _sql(settings, "UPDATE download_settings SET blocked_until = NULL")
    before = memberships()
    errors["alt"] = BOT_CHECK_ERROR
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _poll(sessionmaker, settings, 1, listing, prober=prober)
    assert probed[2:] == [("member", "alt")]
    assert memberships() == before
    [failed] = [r for r in caplog.records if r.getMessage() == "membership.check_failed"]
    assert failed.levelno == logging.WARNING
    assert (_fields(failed)["profile"], _fields(failed)["video"]) == ("alt", "member")
    assert "not a bot" in _fields(failed)["reason"]
    assert "# alt" not in caplog.text and "# main" not in caplog.text

    # Without a present member profile, the members-only videos are skipped again.
    (settings.cookies_dir / "main.txt").unlink()
    errors["alt"] = MEMBERS_ONLY_ERROR
    await _poll(sessionmaker, settings, 1, listing, prober=prober)
    assert await queued() == ["public"]
    await engine.dispose()


@pytest.mark.anyio
async def test_a_playlist_gone_from_the_channel_disables_its_child(settings):
    _parent(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    # An empty listing proves nothing about the playlists.
    await _poll(sessionmaker, settings, 1, {**PLAYLISTS_LISTING, "entries": []})
    assert all(c.enabled for c in await _children(sessionmaker))

    shorter = {**PLAYLISTS_LISTING, "entries": PLAYLISTS_LISTING["entries"][:2]}
    await _poll(sessionmaker, settings, 1, shorter)
    assert [(c.url, c.enabled, c.last_error) for c in await _children(sessionmaker)] == [
        (f"{PL}A", True, None),
        (f"{PL}B", True, None),
        (f"{PL}C", False, "playlist no longer listed on the channel"),
    ]
    await engine.dispose()


@pytest.mark.anyio
async def test_an_empty_listing_keeps_the_last_titles(settings):
    _sql(
        settings,
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', true, 360)",
    )
    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [{"id": "a", "title": UTAWAKU, "live_status": "was_live"}],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, listing)
    await _poll(sessionmaker, settings, 1, {**listing, "entries": []})
    with _api(settings) as client:
        preview = client.post(
            "/api/sources/1/title-preview", json={"include_words": [], "exclude_words": []}
        ).json()
    assert preview["total"] == 1
    await engine.dispose()


@pytest.mark.anyio
async def test_a_word_change_reaches_a_cached_detection_without_a_new_probe(settings):
    from savetube.live import check_channel

    _watch(settings)
    _sql(settings, "UPDATE source SET exclude_words = '{Artemis}'")
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    probed = []

    def probe(video_id):
        probed.append(video_id)
        return UPCOMING

    seen = {}

    async def tick():
        await check_channel(
            sessionmaker,
            NASA,
            fetch_feed=lambda c: _feed(UPCOMING["id"]),
            probe=probe,
            probe_live=_not_live,
            list_streams=_no_streams,
            seen=seen,
        )

    await tick()
    assert await _record_jobs(sessionmaker) == []
    _sql(settings, "UPDATE source SET exclude_words = '{}'")
    await tick()
    jobs = await _record_jobs(sessionmaker)
    assert [(j.video_id, j.scheduled_at) for j in jobs] == [
        (UPCOMING["id"], datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
    ]
    assert probed == [UPCOMING["id"]]
    await engine.dispose()


@pytest.mark.anyio
async def test_the_streams_tab_decides_live_words_when_it_watches(settings):
    from savetube.live import check_channel

    _watch(settings)
    _sql(
        settings,
        "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes,"
        " include_words)"
        f" VALUES ('https://www.youtube.com/channel/{NASA}/videos', 'tab', '{NASA}', true, 360,"
        "  '{}')",
        # The /streams source from _watch now wants only Artemis.
        "UPDATE source SET include_words = '{Artemis}' WHERE url LIKE '%/streams'",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    async def tick():
        await check_channel(
            sessionmaker,
            NASA,
            fetch_feed=lambda c: _feed(),
            probe=lambda v: pytest.fail("nothing to probe"),
            probe_live=lambda c: LIVE_NOW,
            list_streams=_no_streams,
            seen={},
        )

    # The /videos source would pass the ISS stream, but /streams decides.
    await tick()
    assert await _record_jobs(sessionmaker) == []
    _sql(settings, "UPDATE source SET enabled = false WHERE url LIKE '%/streams'")
    await tick()
    assert [j.video_id for j in await _record_jobs(sessionmaker)] == [LIVE_NOW["id"]]
    await engine.dispose()


def test_api_channel_playlists_is_a_post_with_a_timeout(settings, monkeypatch):
    import time

    from savetube.web import api

    monkeypatch.setattr(api, "CHANNEL_PLAYLISTS_TIMEOUT", 0.2)

    def slow(url, limit):
        time.sleep(1)
        return PLAYLISTS_LISTING

    url = {"url": f"https://www.youtube.com/channel/{VESP}"}
    with _api(settings, slow) as client:
        assert client.post("/api/channel-playlists", json=url).status_code == 504
        assert client.get("/api/channel-playlists", params=url).status_code == 405
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    with TestClient(app) as bare:
        assert bare.post("/api/channel-playlists", json=url).status_code == 403


def test_api_words_without_a_row_and_word_limits(settings):
    _sql(
        settings,
        "DELETE FROM global_words",
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
    )
    with _api(settings) as client:
        assert client.get("/api/words").json() == {"include_words": [], "exclude_words": []}
        too_many = {"include_words": [f"w{i}" for i in range(201)], "exclude_words": []}
        too_long = {"include_words": ["x" * 101], "exclude_words": []}
        assert client.put("/api/words", json=too_many).status_code == 422
        assert client.put("/api/words", json=too_long).status_code == 422
        assert client.patch("/api/channels/UC1", json=too_long).status_code == 422
        ok = {"include_words": [f"w{i}" for i in range(200)], "exclude_words": ["x" * 100]}
        assert client.put("/api/words", json=ok).status_code == 200
        assert client.get("/api/words").json()["exclude_words"] == ["x" * 100]


def _job_rows(settings, *extra):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'videos', 't', 'pending'), ('busy1', 'UC1', 'videos', 't', 'pending'),"
        " ('busy2', 'UC1', 'lives', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts, queue_job_id) VALUES"
        " (1, 'archive', 'queued', 'v1', 0, 500)",
        *extra,
    )


def _closed_window_sql():
    """A window opening two hours from now, so now is outside it."""
    from savetube.limits import TIMEZONE

    now = datetime.now(TIMEZONE)
    start = (now + timedelta(hours=2)).strftime("%H:%M")
    end = (now + timedelta(hours=3)).strftime("%H:%M")
    return (
        f"UPDATE download_settings SET window_start = '{start}', window_end = '{end}',"
        " window_enabled = true"
    )


class _Runner:
    def __init__(self):
        self.params = None

    async def __call__(self, url, params, report):
        from savetube.jobs import DownloadFailed

        self.params = params
        raise DownloadFailed("stopped by the test")


async def _run(settings, *, live=False):
    from savetube.jobs import run

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    runner = _Runner()
    try:
        await run(1, settings, live=live, runner=runner, sessionmaker=sessionmaker)
    except Exception as e:
        if type(e).__name__ != "DownloadFailed":
            raise
    async with sessionmaker() as session:
        job = await session.get(Job, 1)
        queue_rows = await session.scalar(sa.text("SELECT count(*) FROM procrastinate_jobs"))
    await engine.dispose()
    return job, runner, queue_rows


@pytest.mark.anyio
async def test_an_archive_outside_the_window_goes_back_to_waiting(settings):
    _job_rows(settings, _closed_window_sql())
    job, runner, queue_rows = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.attempts == 0
    # The dispatcher takes it again. Nothing new is queued.
    assert job.queue_job_id is None
    assert queue_rows == 0


@pytest.mark.anyio
async def test_an_archive_without_a_free_slot_goes_back_to_waiting(settings):
    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1)",
    )
    job, runner, queue_rows = await _run(settings)
    assert runner.params is None and job.state is JobState.QUEUED
    assert job.queue_job_id is None
    assert queue_rows == 0


@pytest.mark.anyio
async def test_an_admitted_archive_runs_with_the_rate_limit(settings):
    _job_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 2, rate_limit_bytes = 1000000",
        # A running recording takes no archive slot.
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1), (3, 'record', 'running', 'busy2', 1)",
    )
    job, runner, _ = await _run(settings)
    assert runner.params["ratelimit"] == 1000000
    assert job.attempts == 1 and job.started_at is not None
    assert job.state is JobState.FAILED  # the test runner fails every download


@pytest.mark.anyio
async def test_a_recording_ignores_every_download_limit(settings):
    _job_rows(
        settings,
        _closed_window_sql(),
        "UPDATE download_settings SET rate_limit_bytes = 1000",
        "UPDATE job SET kind = 'record' WHERE id = 1",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1)",
    )
    job, runner, _ = await _run(settings, live=True)
    assert runner.params is not None and "ratelimit" not in runner.params


def _queue_row(settings, status="todo"):
    """A procrastinate job row, as the real queue would add it."""
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.begin() as c:
        queue_job_id = c.execute(
            sa.text(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args)"
                " VALUES ('archive', 'archive', '{}') RETURNING id"
            )
        ).scalar()
        if status != "todo":
            c.execute(
                sa.text(
                    "UPDATE procrastinate_jobs SET status = CAST(:s AS procrastinate_job_status)"
                    " WHERE id = :id"
                ),
                {"s": status, "id": queue_job_id},
            )
    engine.dispose()
    return queue_job_id


async def _dispatch(settings, now=None):
    from savetube.jobs import dispatch

    enqueued = []

    async def enqueue(job_id):
        enqueued.append(job_id)
        return _queue_row(settings)

    engine = make_engine(settings.sqlalchemy_url)
    async with make_sessionmaker(engine)() as session:
        await dispatch(session, now or datetime.now(UTC), enqueue)
    async with make_sessionmaker(engine)() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
    await engine.dispose()
    return enqueued, jobs


def _waiting_rows(settings, *extra):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('a', 'UC1', 'videos', 't', 'pending'), ('b', 'UC1', 'videos', 't', 'pending'),"
        " ('c', 'UC1', 'videos', 't', 'pending'), ('run', 'UC1', 'videos', 't', 'downloading'),"
        " ('live', 'UC1', 'lives', 't', 'downloading'), ('soon', 'UC1', 'lives', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (10, 'archive', 'running', 'run', 1), (11, 'record', 'running', 'live', 1),"
        " (12, 'archive', 'queued', 'a', 0), (13, 'archive', 'queued', 'b', 0),"
        " (14, 'archive', 'queued', 'c', 0), (15, 'record', 'queued', 'soon', 0)",
        *extra,
    )


@pytest.mark.anyio
async def test_dispatch_queues_the_oldest_waiting_archive_jobs_up_to_the_free_slots(settings):
    # One archive runs, so max_concurrent 2 leaves one slot. A recording takes none.
    _waiting_rows(settings, "UPDATE download_settings SET max_concurrent = 2")
    enqueued, jobs = await _dispatch(settings)
    assert enqueued == [12]
    assert jobs[12].queue_job_id is not None and jobs[12].state is JobState.QUEUED
    assert jobs[13].queue_job_id is None and jobs[15].queue_job_id is None

    # The dispatched job holds its slot until a worker takes it.
    enqueued, _ = await _dispatch(settings)
    assert enqueued == []

    # A queue job that ended without a worker taking the `job` row frees the slot.
    _sql(
        settings,
        "UPDATE procrastinate_jobs SET status = 'failed'",
    )
    enqueued, jobs = await _dispatch(settings)
    assert enqueued == [12]


@pytest.mark.anyio
async def test_dispatch_with_max_concurrent_1_and_one_running_queues_nothing(settings):
    _waiting_rows(settings)
    enqueued, jobs = await _dispatch(settings)
    assert enqueued == []
    assert all(j.deferred_until is None for j in jobs.values())


@pytest.mark.anyio
async def test_dispatch_waits_for_the_window_and_shows_its_start(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5,"
        " window_start = '23:00', window_end = '05:00', window_enabled = true",
    )
    # 12:00 in Seoul, eleven hours before the window opens.
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    opens = datetime(2026, 10, 21, 23, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, noon)
    assert enqueued == []
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [opens, opens, opens]
    assert jobs[15].deferred_until is None

    enqueued, jobs = await _dispatch(settings, opens)
    assert enqueued == [12, 13, 14]
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [None, None, None]


def test_api_download_settings_and_job_deferral(settings):
    _job_rows(settings, "UPDATE job SET deferred_until = '2026-10-21T14:00:00Z'")
    with _api(settings) as client:
        assert client.get("/api/download-settings").json() == {
            "max_concurrent": 1,
            "rate_limit_bytes": None,
            "window_start": None,
            "window_end": None,
            "window_enabled": False,
            "daily_limit": None,
            "cookie_profile": None,
            "timezone": "Asia/Seoul",
            "downloaded_today": 0,
            "blocked_until": None,
            "block_count": 0,
            "paused": False,
        }
        body = {
            "max_concurrent": 3,
            "rate_limit_bytes": 2000000,
            "window_start": "23:00",
            "window_end": "05:00",
            "window_enabled": True,
            "daily_limit": None,
            "cookie_profile": None,
        }
        put = client.put("/api/download-settings", json=body)
        assert put.status_code == 200
        unblocked = {"blocked_until": None, "block_count": 0, "paused": False}
        today = {"timezone": "Asia/Seoul", "downloaded_today": 0}
        assert put.json() == body | today | unblocked
        assert client.get("/api/download-settings").json() == put.json()
        for bad in (
            body | {"window_end": None},
            body | {"window_end": "23:00"},
            body | {"window_start": "25:00"},
            body | {"max_concurrent": 0},
            body | {"max_concurrent": 11},
            body | {"rate_limit_bytes": 0},
        ):
            assert client.put("/api/download-settings", json=bad).status_code == 422, bad
        open_all_day = body | {"window_start": None, "window_end": None, "window_enabled": False}
        assert client.put("/api/download-settings", json=open_all_day).status_code == 200

        [job] = client.get("/api/jobs").json()
        assert job["deferred_until"] == "2026-10-21T14:00:00Z"


def test_api_cookie_profiles_the_general_profile_and_memberships(settings, caplog):
    import logging

    _sql(
        settings,
        f"INSERT INTO channel (id, name, dir_name) VALUES ('{VESP}', 'V', 'V [{VESP}]')",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        f" ('{VESP}', 'main', true, '2026-10-21T01:00:00Z', 'm1'),"
        f" ('{VESP}', 'alt', false, '2026-10-21T02:00:00Z', 'm1')",
    )
    with _api(settings) as client:
        assert client.get("/api/cookie-profiles").json() == []
        assert (
            client.put("/api/download-settings", json={"cookie_profile": "main"}).status_code == 422
        )
        [channel] = client.get("/api/channels").json()
        assert channel["memberships"] == [
            {"profile": "alt", "is_member": False, "checked_at": "2026-10-21T02:00:00Z"},
            {"profile": "main", "is_member": True, "checked_at": "2026-10-21T01:00:00Z"},
        ]
        patched = client.patch(f"/api/channels/{VESP}", json={"watch_lives": True}).json()
        assert patched["memberships"] == channel["memberships"]

    # The scheduler records what its mount holds. The web reads only the table.
    from savetube.cookies import profiles, record_profiles

    async def record(names):
        engine = make_engine(settings.sqlalchemy_url)
        async with make_sessionmaker(engine)() as session:
            await record_profiles(session, names, datetime.now(UTC))
            await session.commit()
        await engine.dispose()

    asyncio.run(record(["gone", "main"]))
    asyncio.run(record(profiles(_profiles(settings, "main", "throwaway"))))
    with _api(settings) as client:
        assert settings.cookies_dir is None
        assert client.get("/api/cookie-profiles").json() == [
            {"name": "main"},
            {"name": "throwaway"},
        ]
        with caplog.at_level(logging.INFO, logger="savetube"):
            put = client.put("/api/download-settings", json={"cookie_profile": "throwaway"})
        assert put.status_code == 200 and put.json()["cookie_profile"] == "throwaway"
        assert client.get("/api/download-settings").json()["cookie_profile"] == "throwaway"
        assert "# throwaway" not in caplog.text and "# throwaway" not in put.text
        [line] = [r for r in caplog.records if r.getMessage() == "settings.download"]
        assert _fields(line)["cookie_profile"] == "throwaway"
        put = client.put("/api/download-settings", json={"cookie_profile": "gone"})
        assert put.status_code == 422
        assert client.put("/api/download-settings", json={}).json()["cookie_profile"] is None


@pytest.mark.anyio
async def test_a_source_lists_without_downloading_until_archiving_is_on(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('stored', '{VESP}', 'lives', 's', 'complete')",
    )
    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [
            {"id": "stored", "title": UTAWAKU, "live_status": "was_live"},
            {"id": "new", "title": UTAWAKU, "live_status": "was_live"},
        ],
    }
    with _api(settings) as client:
        [source] = client.post(
            "/api/sources",
            json={"url": f"https://www.youtube.com/channel/{VESP}/streams"},
        ).json()
    assert source["archiving"] is False

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, source["id"], listing)
    assert await _archive_jobs(sessionmaker) == []
    async with sessionmaker() as session:
        assert await session.get(Video, "new") is None
        linked = set(await session.scalars(sa.select(VideoSource.video_id)))
    assert linked == {"stored"}
    with _api(settings) as client:
        preview = client.post(
            f"/api/sources/{source['id']}/title-preview",
            json={"include_words": [], "exclude_words": []},
        ).json()
        assert preview["total"] == 2
        on = client.patch(f"/api/sources/{source['id']}", json={"archiving": True}).json()
        assert on["archiving"] is True and on["last_polled_at"] is None

    await _poll(sessionmaker, settings, source["id"], listing)
    assert await _archive_jobs(sessionmaker) == ["new"]
    await engine.dispose()


@pytest.mark.anyio
async def test_a_parent_passes_archiving_to_its_children(settings):
    _parent(
        settings,
        "UPDATE source SET archiving = false WHERE id = 1",
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes, parent_id,"
        f" archiving) VALUES (2, '{PL}A', 'playlist', true, 120, 1, false)",
        "SELECT setval('source_id_seq', 2)",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    assert [c.archiving for c in await _children(sessionmaker)] == [False, False, False]
    with _api(settings) as client:
        client.patch("/api/sources/1", json={"archiving": True})
    assert [c.archiving for c in await _children(sessionmaker)] == [True, True, True]
    await engine.dispose()


@pytest.mark.anyio
async def test_reclaim_interrupts_the_jobs_of_a_dead_worker_and_keeps_their_parts(settings):
    from savetube.jobs import reclaim_stalled

    _job_rows(
        settings,
        "UPDATE job SET state = 'running', attempts = 1, started_at = now() WHERE id = 1",
        "UPDATE video SET state = 'downloading' WHERE id = 'v1'",
        "INSERT INTO job (id, kind, state, video_id, attempts, queue_job_id) VALUES"
        " (2, 'archive', 'queued', 'busy1', 0, 701),"
        " (3, 'archive', 'running', 'busy2', 1, 702)",
        "SELECT setval('job_id_seq', 200)",
    )
    incoming = settings.incoming_root / "v1" / ".tmp"
    incoming.mkdir(parents=True)
    (incoming / "v1.f251.webm.part").write_bytes(b"x")
    finished = []

    async def stalled():
        return [(700, 1), (701, 2)]

    async def finish(queue_job_id):
        finished.append(queue_job_id)

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await reclaim_stalled(sessionmaker, settings, stalled=stalled, finish=finish)
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
        video = await session.get(Video, "v1")
    # The stopped run is a row of its own. The job waits for the dispatcher again.
    [history] = [j for j in jobs.values() if j.id not in (1, 2, 3)]
    assert (history.state, history.video_id, history.kind) == ("interrupted", "v1", JobKind.ARCHIVE)
    assert history.log_tail == "worker stopped during the download"
    assert history.attempts == 1
    assert history.started_at is not None and history.finished_at is not None
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    # A dispatched job no worker ran goes back to waiting, with no history row.
    assert (jobs[2].state, jobs[2].queue_job_id) == (JobState.QUEUED, None)
    assert jobs[3].state is JobState.RUNNING
    assert video.state is VideoState.PENDING
    assert (incoming / "v1.f251.webm.part").read_bytes() == b"x"
    assert finished == [700, 701]
    await engine.dispose()


@pytest.mark.anyio
async def test_playlist_sources_carry_their_title(settings):
    _parent(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    assert [c.title for c in await _children(sessionmaker)] == [
        "◀︎YOMI Stream▶︎",
        "◀︎KASUKA Stream▶︎",
        "◀︎BD LIVE▶︎",
    ]
    renamed = {
        **PLAYLISTS_LISTING,
        "entries": [
            {**e, "title": "YOMI 歌枠"} if e["id"].endswith("A") else e
            for e in PLAYLISTS_LISTING["entries"]
        ],
    }
    await _poll(sessionmaker, settings, 1, renamed)
    a, b, _ = await _children(sessionmaker)
    assert a.title == "YOMI 歌枠"

    await _poll(sessionmaker, settings, b.id, {"title": "◀︎KASUKA 歌枠▶︎", "entries": []})
    with _api(settings) as client:
        titles = {s["id"]: s["title"] for s in client.get("/api/sources").json()}
    assert titles[1] is None
    assert titles[b.id] == "◀︎KASUKA 歌枠▶︎"
    await engine.dispose()


def test_migration_0005_moves_rule_settings_onto_channels(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.begin() as c:
        c.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = alembic_config(settings.sqlalchemy_url)
    command.upgrade(cfg, "0004")
    _sql(
        settings,
        "INSERT INTO rule (id, name, types, date_after, format, use_cookies, watch_lives) VALUES"
        " (1, 'a', '{videos}', '2026-02-01', 'f1', false, false),"
        " (2, 'b', '{lives}', '2026-01-01', 'f2', true, true),"
        " (3, 'c', '{videos}', NULL, 'f3', false, false)",
        "INSERT INTO channel (id, name, dir_name) VALUES"
        " ('UC1', 'a', 'a'), ('UC2', 'b', 'b'), ('UC3', 'c', 'c')",
        "INSERT INTO source (id, url, kind, channel_id, rule_id, enabled, poll_interval_minutes)"
        " VALUES (1, 'https://www.youtube.com/@a/videos', 'tab', 'UC1', 1, true, 360),"
        "        (2, 'https://www.youtube.com/@a/streams', 'tab', 'UC1', 2, true, 360),"
        "        (3, 'https://www.youtube.com/@b/videos', 'tab', 'UC2', 3, true, 360),"
        "        (4, 'https://www.youtube.com/@b/streams', 'tab', 'UC2', 1, true, 360),"
        "        (5, 'https://www.youtube.com/playlist?list=PL1', 'playlist', NULL, 2, true, 360)",
    )
    command.upgrade(cfg, "0009")
    with engine.connect() as c:
        rows = c.execute(
            sa.text(
                "SELECT id, date_after, format, use_cookies, watch_lives FROM channel ORDER BY id"
            )
        ).all()
        rule_table = c.execute(sa.text("SELECT to_regclass('rule')")).scalar()
        columns = set(
            c.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'source'"
                )
            ).scalars()
        )
    engine.dispose()
    assert [tuple(r) for r in rows] == [
        # Either flag on wins, the earliest cutoff wins, the lowest source id's format.
        ("UC1", date(2026, 1, 1), "f1", True, True),
        # No cutoff is the earliest of all.
        ("UC2", None, "f3", False, False),
        ("UC3", None, "bv*+ba/b", False, False),
    ]
    assert rule_table is None
    assert "rule_id" not in columns


def _scanner(failing=None):
    """A fake listing per URL, recording each call. `failing` maps a tab to its error."""
    from yt_dlp.utils import DownloadError

    calls = []
    head = {"channel_id": VESP, "channel": "VESPERBELL", "uploader_id": "@VESPERBELL"}
    listings = {
        "videos": {**head, "entries": [{"id": "cover", "title": "IRIS OUT (Cover)"}]},
        "shorts": {**head, "entries": [{"id": "short", "title": "#shorts 【歌枠】切り抜き"}]},
        "streams": {
            **head,
            "entries": [
                {"id": "utawaku", "title": UTAWAKU},
                {"id": "notice", "title": "【歌枠】重大告知アリ"},
            ],
        },
        "playlists": PLAYLISTS_LISTING,
    }

    def scan(url, limit):
        calls.append((url, limit))
        tab = url.rsplit("/", 1)[1]
        if failing and tab in failing:
            raise DownloadError(failing[tab])
        return listings[tab]

    return scan, calls


def test_api_channel_scan_lists_the_first_page_of_each_tab_and_the_playlists(settings):
    scan, calls = _scanner({"shorts": "ERROR: This channel does not have a shorts tab"})
    with _api(settings, scan) as client:
        response = client.post(
            "/api/channel-scan", json={"url": "https://www.youtube.com/@VESPERBELL/videos"}
        )
        assert response.status_code == 200
        body = response.json()
        assert sorted(calls) == [
            ("https://www.youtube.com/@VESPERBELL/playlists", None),
            ("https://www.youtube.com/@VESPERBELL/shorts", 30),
            ("https://www.youtube.com/@VESPERBELL/streams", 30),
            ("https://www.youtube.com/@VESPERBELL/videos", 30),
        ]
        assert (body["url"], body["channel_id"], body["handle"], body["name"]) == (
            "https://www.youtube.com/@VESPERBELL",
            VESP,
            "@VESPERBELL",
            "VESPERBELL",
        )
        assert body["tabs"] == {
            "videos": [{"video_id": "cover", "title": "IRIS OUT (Cover)"}],
            # The channel has no Shorts tab.
            "shorts": None,
            "streams": [
                {"video_id": "utawaku", "title": UTAWAKU},
                {"video_id": "notice", "title": "【歌枠】重大告知アリ"},
            ],
        }
        assert [p["title"] for p in body["playlists"]] == [
            "◀︎YOMI Stream▶︎",
            "◀︎KASUKA Stream▶︎",
            "◀︎BD LIVE▶︎",
        ]
        assert body["suggestions"] == [{"word": "歌枠", "count": 2}]
        bad = client.post(
            "/api/channel-scan", json={"url": "https://www.youtube.com/playlist?list=PL1"}
        )
        assert bad.status_code == 422
        # Nothing is stored by a scan.
        assert client.get("/api/channels").json() == []

    gone, _ = _scanner(dict.fromkeys(["videos", "shorts", "streams", "playlists"], "gone"))
    with _api(settings, gone) as client:
        response = client.post("/api/channel-scan", json={"url": "https://www.youtube.com/@x"})
    assert response.status_code == 502 and response.json()["detail"] == "gone"


def test_api_channel_scan_times_out(settings, monkeypatch):
    import time

    from savetube.web import api

    monkeypatch.setattr(api, "CHANNEL_PLAYLISTS_TIMEOUT", 0.2)

    def slow(url, limit):
        time.sleep(1)
        return {}

    with _api(settings, slow) as client:
        response = client.post("/api/channel-scan", json={"url": "https://www.youtube.com/@x"})
    assert response.status_code == 504


def test_api_title_preview_merges_stored_global_and_channel_words(settings):
    _sql(
        settings,
        "UPDATE global_words SET exclude_words = '{告知}'",
        "INSERT INTO channel (id, name, dir_name, exclude_words) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]', '{{Cover}}')",
    )
    titles = [UTAWAKU, "【歌枠】重大告知アリ", "IRIS OUT (Cover)", GUERRILLA]
    with _api(settings) as client:

        def kept(**body):
            response = client.post(
                "/api/title-preview",
                json={"titles": titles, "include_words": [], "exclude_words": []} | body,
            )
            assert response.status_code == 200
            assert [t["title"] for t in response.json()] == titles
            return [t["kept"] for t in response.json()]

        # A channel not stored yet, as at onboarding: only the global lists.
        assert kept() == [True, False, True, True]
        assert kept(include_words=["歌枠 "]) == [True, False, False, False]
        assert kept(channel_id=VESP) == [True, False, False, True]
        assert kept(channel_id="UCnew", exclude_words=["ゲリラ"]) == [True, False, True, False]


def _channel_body(**extra):
    return {
        "url": "https://www.youtube.com/@VESPERBELL",
        "channel_id": VESP,
        "handle": "@VESPERBELL",
        "name": "VESPERBELL",
        "tabs": ["streams", "videos"],
        "playlists": [{"url": f"{PL}B", "title": "◀︎KASUKA Stream▶︎"}],
        "watch_lives": True,
        "include_words": ["歌枠", " 歌枠"],
        "exclude_words": ["告知"],
        "date_after": "2026-01-01",
        "format": "wa",
    } | extra


def test_api_creates_a_channel_with_its_settings_and_archiving_sources(settings):
    with _api(settings) as client:
        response = client.post("/api/channels", json=_channel_body())
        assert response.status_code == 201
        body = response.json()
        assert {k: body[k] for k in ("id", "handle", "name", "dir_name")} == {
            "id": VESP,
            "handle": "@VESPERBELL",
            "name": "VESPERBELL",
            "dir_name": f"VESPERBELL [{VESP}]",
        }
        assert (body["include_words"], body["exclude_words"]) == (["歌枠"], ["告知"])
        assert (body["date_after"], body["format"], body["watch_lives"]) == (
            "2026-01-01",
            "wa",
            True,
        )
        assert body["memberships"] == []
        assert [
            (s["url"], s["kind"], s["title"], s["channel_id"], s["archiving"], s["include_words"])
            for s in body["sources"]
        ] == [
            ("https://www.youtube.com/@VESPERBELL/streams", "tab", None, VESP, True, []),
            ("https://www.youtube.com/@VESPERBELL/videos", "tab", None, VESP, True, []),
            (f"{PL}B", "playlist", "◀︎KASUKA Stream▶︎", VESP, True, []),
        ]
        # Other uploaders' playlist entries download by default. A tab never takes the flag.
        assert [s["other_channels"] for s in body["sources"]] == [False, False, True]
        again = client.post("/api/channels", json=_channel_body(tabs=["videos"], playlists=[]))
        assert again.status_code == 409

        assert (
            client.post(
                "/api/channels", json=_channel_body(tabs=[], playlists=[], watch_lives=False)
            ).status_code
            == 422
        )
        only_playlists = _channel_body(tabs=[], all_playlists=True)
        assert client.post("/api/channels", json=only_playlists).status_code == 422

        # A tab added later takes the channel the other tabs already found.
        [shorts] = client.post(
            "/api/sources", json={"url": "https://www.youtube.com/@VESPERBELL/shorts"}
        ).json()
        assert shorts["channel_id"] == VESP

        patched = client.patch(
            f"/api/channels/{VESP}",
            json={"date_after": None, "format": "bv*+ba/b"},
        )
        assert patched.status_code == 200
        channel = patched.json()
        assert (channel["date_after"], channel["format"]) == (None, "bv*+ba/b")
        # An omitted field stays.
        assert channel["watch_lives"] is True and channel["include_words"] == ["歌枠"]
        assert client.patch(f"/api/channels/{VESP}", json={"format": ""}).status_code == 422

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    async def watched():
        from savetube.live import watched_channels

        async with sessionmaker() as session:
            return await watched_channels(session)

    assert asyncio.run(watched()) == [VESP]


def test_api_tracks_all_playlists_of_a_new_channel(settings):
    with _api(settings) as client:
        body = client.post(
            "/api/channels",
            json=_channel_body(
                tabs=["videos"], all_playlists=True, playlists=[], other_channels=False
            ),
        ).json()
    assert [
        (s["url"], s["kind"], s["archiving"], s["other_channels"]) for s in body["sources"]
    ] == [
        ("https://www.youtube.com/@VESPERBELL/videos", "tab", True, False),
        ("https://www.youtube.com/@VESPERBELL/playlists", "playlists", True, False),
    ]


@pytest.mark.anyio
async def test_every_poll_takes_the_general_profile(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360),"
        " (2, 'https://www.youtube.com/playlist?list=PLnew', 'playlist', NULL, true, 360)",
    )
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    cookies = []

    def lister(url, cookiefile):
        cookies.append(cookiefile.read_text() if cookiefile else None)
        return {"entries": []}

    # No general profile: cookie-free.
    await poll_source_with(sessionmaker, settings, 1, lister)
    assert cookies == [None]

    _sql(settings, "UPDATE download_settings SET cookie_profile = 'throwaway'")
    # The profile is not in SAVETUBE_COOKIES_DIR.
    await poll_source_with(sessionmaker, settings, 1, lister)
    async with sessionmaker() as session:
        assert (await session.get(Source, 1)).last_error == (
            "RuntimeError: cookie profile throwaway is not in SAVETUBE_COOKIES_DIR"
        )
    assert cookies == [None]

    with_profiles = _profiles(settings, "throwaway", "member")
    for source_id in (1, 2):
        await poll_source_with(sessionmaker, with_profiles, source_id, lister)
    assert cookies == [None, "# throwaway\n", "# throwaway\n"]
    await engine.dispose()


async def poll_source_with(sessionmaker, settings, source_id, lister):
    from savetube.poll import poll_source

    await poll_source(
        sessionmaker, source_id, settings, lister=lister, cancel=_cancel, prober=_no_probe
    )


@pytest.mark.anyio
async def test_a_download_takes_format_from_its_sources_channel(settings):
    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa' WHERE id = 'UC1'",
        "INSERT INTO channel (id, name, dir_name, format) VALUES ('UC2', 'B', 'B [UC2]', 'ba')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/playlist?list=PLb', 'playlist', 'UC2', true, 360)",
    )
    # Not listed by any source: the video's own channel applies.
    job, runner, _ = await _run(settings)
    assert runner.params["format"] == "wa" and "cookiefile" not in runner.params

    _sql(
        settings,
        "INSERT INTO video_source (video_id, source_id) VALUES ('v1', 1)",
        "UPDATE job SET state = 'queued' WHERE id = 1",
    )
    job, runner, _ = await _run(settings)
    assert runner.params["format"] == "ba"


class _CookieRunner(_Runner):
    """Records the cookie file's content, which the run deletes afterwards."""

    async def __call__(self, url, params, report):
        path = params.get("cookiefile")
        self.cookies = await asyncio.to_thread(Path(path).read_text) if path else None
        return await super().__call__(url, params, report)


@pytest.mark.anyio
async def test_a_members_only_download_takes_a_member_profile_and_no_other(settings, caplog):
    import logging

    from savetube.jobs import NO_MEMBER, run

    _job_rows(
        settings,
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('m1', 'UC1', 'members', 'm', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'queued', 'm1', 0)",
        "UPDATE download_settings SET cookie_profile = 'general'",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        " ('UC1', 'b_member', true, now(), 'm1'), ('UC1', 'c_member', true, now(), 'm1'),"
        " ('UC1', 'a_gone', true, now(), 'm1'), ('UC1', 'general', false, now(), 'm1')",
    )
    settings = _profiles(settings, "general", "b_member", "c_member")
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    async def run_job(job_id):
        runner = _CookieRunner()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="savetube"):
            try:
                await run(job_id, settings, live=False, runner=runner, sessionmaker=sessionmaker)
            except Exception as e:
                if type(e).__name__ != "DownloadFailed":
                    raise
        [start] = [r for r in caplog.records if r.getMessage() == "job.start"]
        return getattr(runner, "cookies", "not run"), _fields(start)["cookies"]

    # A public video takes the general profile, never a member one.
    assert await run_job(1) == ("# general\n", "general")
    # The first present member by name. `a_gone` has no file any more.
    assert await run_job(2) == ("# b_member\n", "b_member")

    # No member left: the job fails without a download, as one attempt.
    _sql(
        settings,
        "UPDATE membership SET is_member = false",
        "UPDATE job SET state = 'queued' WHERE id = 2",
    )
    assert await run_job(2) == ("not run", "none")
    async with sessionmaker() as session:
        job = await session.get(Job, 2)
        assert (job.state, job.attempts, job.log_tail) == (JobState.FAILED, 2, NO_MEMBER)
    await engine.dispose()


def test_api_lists_a_channels_archive_jobs_with_titles(settings):
    _job_rows(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC2', 'B', 'B [UC2]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('w1', 'UC2', 'videos', 'other', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'queued', 'w1', 0)",
    )
    with _api(settings) as client:
        jobs = client.get("/api/jobs", params={"channel_id": "UC2"}).json()
    assert [(j["id"], j["title"], j["channel_id"]) for j in jobs] == [(2, "other", "UC2")]


def test_api_jobs_carry_the_channel_name_and_the_failure_reason(settings):
    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail) VALUES"
        " (2, 'archive', 'failed', 'busy1', 1,"
        " E'DownloadError: ERROR: \\r[download] Got error: 10 bytes read, 5 more expected'),"
        " (3, 'archive', 'interrupted', 'busy2', 0, 'worker stopped during the download')",
    )
    with _api(settings) as client:
        jobs = client.get("/api/jobs", params={"state": ["failed", "interrupted"]}).json()
    assert [
        (j["id"], j["title"], j["channel_id"], j["channel_name"], j["reason"]) for j in jobs
    ] == [
        (3, "t", "UC1", "A", "worker stopped"),
        (2, "t", "UC1", "A", "network error"),
    ]
    assert jobs[1]["log_tail"].endswith("10 bytes read, 5 more expected")


def test_migration_0006_hands_waiting_archive_jobs_to_the_dispatcher(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0005")
    done = [_queue_row(settings, "succeeded") for _ in range(3)]
    waiting = _queue_row(settings)
    taken = _queue_row(settings, "doing")
    lost = _queue_row(settings, "failed")
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('a', 'UC1', 'videos', 't', 'pending'), ('b', 'UC1', 'videos', 't', 'pending'),"
        " ('c', 'UC1', 'videos', 't', 'pending'), ('d', 'UC1', 'videos', 't', 'complete')",
        "INSERT INTO job (id, kind, state, video_id, attempts, queue_job_id, deferred_until)"
        " VALUES"
        f" (1, 'archive', 'queued', 'a', 0, {waiting}, now() + interval '1 minute'),"
        f" (2, 'archive', 'queued', 'b', 0, {taken}, NULL),"
        f" (3, 'archive', 'queued', 'c', 0, {lost}, NULL),"
        f" (4, 'archive', 'succeeded', 'd', 1, {done[0]}, NULL)",
    )
    command.upgrade(cfg, "head")
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        queue_rows = dict(c.execute(sa.text("SELECT id, status FROM procrastinate_jobs")).all())
        jobs = {
            r.id: (r.state, r.queue_job_id, r.deferred_until)
            for r in c.execute(sa.text("SELECT id, state, queue_job_id, deferred_until FROM job"))
        }
    # The running task keeps its row. A failed row stays as the record of a failure.
    assert queue_rows == {taken: "doing", lost: "failed"}
    assert jobs == {
        1: ("queued", None, None),
        2: ("queued", taken, None),
        3: ("queued", None, None),
        4: ("succeeded", done[0], None),
    }

    command.downgrade(cfg, "0005")
    with engine.connect() as c:
        states = dict(c.execute(sa.text("SELECT id, state FROM job")).all())
    engine.dispose()
    # The old code queues only through a poll, which skips a video with a
    # queued job, so a waiting job without a queue job is cancelled for the
    # next poll to queue it again.
    assert states == {1: "cancelled", 2: "queued", 3: "cancelled", 4: "succeeded"}


class _ProgressRunner:
    """Reports one progress update, then fails like a stopped download."""

    PROGRESS = {
        "format": "140",
        "pct": 25.0,
        "done": 1048576,
        "total": 4194304,
        "speed": 524288.0,
        "eta": 6,
    }

    async def __call__(self, url, params, report):
        from savetube.jobs import DownloadFailed

        await report(self.PROGRESS)
        raise DownloadFailed("stopped by the test")


@pytest.mark.anyio
async def test_a_download_stores_its_progress_on_the_job_row_for_the_api(settings):
    from savetube.jobs import DownloadFailed, run

    _job_rows(settings)
    engine = make_engine(settings.sqlalchemy_url)
    with pytest.raises(DownloadFailed):
        await run(
            1,
            settings,
            live=False,
            runner=_ProgressRunner(),
            sessionmaker=make_sessionmaker(engine),
        )
    await engine.dispose()
    with _api(settings) as client:
        [job] = client.get("/api/jobs", params={"channel_id": "UC1"}).json()
    assert job["progress"] == _ProgressRunner.PROGRESS


def _fields(record):
    return dict(getattr(record, "savetube_fields", {}))


@pytest.mark.anyio
async def test_a_failed_archive_logs_its_attempt_and_the_third_is_an_error(settings, caplog):
    import logging

    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (9, 'archive', 'failed', 'v1', 1)",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _run(settings)
    events = {r.getMessage(): r for r in caplog.records if r.name == "savetube.jobs"}
    start = _fields(events["job.start"])
    assert (start["job"], start["video"], start["channel"], start["title"]) == (1, "v1", "A", "t")
    assert start["attempt"] == "2/3" and start["cookies"] == "none"
    failed = events["job.failed"]
    assert failed.levelno == logging.WARNING
    assert _fields(failed)["attempt"] == "2/3"
    assert _fields(failed)["reason"] == "DownloadFailed: stopped by the test"
    assert failed.exc_info is None

    caplog.clear()
    # The poll's retry is a new job row, after two failed ones.
    _sql(
        settings,
        "UPDATE job SET id = 10 WHERE id = 1",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (1, 'archive', 'queued', 'v1', 0)",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        await _run(settings)
    [failed] = [r for r in caplog.records if r.getMessage() == "job.failed"]
    assert failed.levelno == logging.ERROR and _fields(failed)["attempt"] == "3/3"


@pytest.mark.anyio
async def test_poll_done_counts_each_skip_reason(settings, caplog):
    import logging

    from savetube.poll import poll_source

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name, date_after, include_words) VALUES"
        " ('UC1', 'Chan', 'Chan [UC1]', '2026-01-01', '{keep}')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@chan/videos', 'tab', 'UC1', true, 360)",
    )
    listing = {
        "channel_id": "UC1",
        "channel": "Chan",
        "entries": [
            {"id": "a", "title": "keep new", "timestamp": 1789948800},
            {"id": "b", "title": "drop me"},
            {"id": "c", "title": "keep old", "timestamp": 1735689600},
            {"id": "d", "title": "keep members", "availability": "subscriber_only"},
            {"id": "e", "title": "keep soon", "live_status": "is_upcoming"},
        ],
    }
    engine = make_engine(settings.sqlalchemy_url)
    with caplog.at_level(logging.INFO, logger="savetube"):
        await poll_source(
            make_sessionmaker(engine),
            1,
            settings,
            lister=lambda u, c: listing,
            cancel=_cancel,
            prober=_no_probe,
        )
    await engine.dispose()
    events = {r.getMessage(): _fields(r) for r in caplog.records}
    assert events["poll.start"]["url"] == "https://www.youtube.com/@chan/videos"
    done = events["poll.done"]
    done.pop("took")
    assert done == {
        "source": 1,
        "listed": 5,
        "passed": 1,
        "queued": 1,
        "stored": 0,
        "in_flight": 0,
        "gave_up": 0,
        "cancelled": 0,
        "skip_words": 1,
        "skip_members": 1,
        "skip_other": 0,
        "skip_date": 1,
        "skip_live": 1,
        "skip_private": 0,
    }


# The thumbnails of a channel tab listing, as yt-dlp lists @VESPERBELL/videos.
AVATAR = "https://yt3.googleusercontent.com/LW2I=s900-c-k-c0x00ffffff-no-rj"
CHANNEL_THUMBNAILS = [
    {
        "url": "https://yt3.googleusercontent.com/MUU0=w1060-fcrop64",
        "height": 175,
        "width": 1060,
        "id": "0",
    },
    {"url": "https://yt3.googleusercontent.com/MUU0=s0", "id": "banner_uncropped"},
    {"url": AVATAR, "height": 900, "width": 900, "id": "7"},
    {"url": "https://yt3.googleusercontent.com/LW2I=s0", "id": "avatar_uncropped"},
]


def test_api_scan_and_create_carry_the_channel_avatar(settings):
    scan, _ = _scanner()

    def with_avatar(url, limit):
        listing = scan(url, limit)
        return {**listing, "thumbnails": CHANNEL_THUMBNAILS}

    with _api(settings, with_avatar) as client:
        body = client.post(
            "/api/channel-scan", json={"url": "https://www.youtube.com/@VESPERBELL"}
        ).json()
        assert body["avatar_url"] == AVATAR
        created = client.post(
            "/api/channels", json=_channel_body(avatar_url=body["avatar_url"])
        ).json()
        assert created["avatar_url"] == AVATAR
        assert client.get("/api/channels").json()[0]["avatar_url"] == AVATAR


@pytest.mark.anyio
async def test_a_tab_poll_gives_an_existing_channel_its_avatar(settings):
    from savetube.poll import poll_source

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Chan', 'Chan [UC1]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@chan/videos', 'tab', 'UC1', true, 360)",
    )
    listing = {
        "channel_id": "UC1",
        "channel": "Chan",
        "thumbnails": CHANNEL_THUMBNAILS,
        "entries": [],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await poll_source(
        sessionmaker, 1, settings, lister=lambda u, c: listing, cancel=_cancel, prober=_no_probe
    )
    async with sessionmaker() as session:
        assert (await session.get(Channel, "UC1")).avatar_url == AVATAR
    await engine.dispose()


def test_api_lists_a_channels_stored_videos_by_state(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'videos', 'stored', 'complete'), ('v2', 'UC1', 'videos', 'p', 'pending')",
    )
    with _api(settings) as client:
        page = client.get("/api/videos", params={"channel_id": "UC1", "state": "complete"}).json()
    assert [v["id"] for v in page["items"]] == ["v1"] and page["total"] == 1


def test_api_filters_jobs_by_several_states(settings):
    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1), (3, 'archive', 'failed', 'busy2', 1)",
    )
    with _api(settings) as client:
        active = client.get("/api/jobs", params={"state": ["running", "queued"]}).json()
        failed = client.get("/api/jobs", params={"state": "failed"}).json()
    assert [j["id"] for j in active] == [2, 1]
    assert [j["id"] for j in failed] == [3]


def test_a_channel_page_sees_videos_its_tabs_list_from_another_uploader(settings):
    # A collaboration lists on the channel's tab, but its info names the other
    # channel, so it is stored under that one.
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]'),"
        " ('UC2', 'B', 'B [UC2]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@a/videos', 'tab', 'UC1', true, 360)",
        "INSERT INTO video (id, channel_id, type, title, state, upload_date) VALUES"
        " ('own', 'UC1', 'videos', 'own', 'complete', '2026-01-02'),"
        " ('collab', 'UC2', 'videos', 'collab', 'complete', '2026-01-01'),"
        " ('elsewhere', 'UC2', 'videos', 'b only', 'complete', '2026-01-03')",
        "INSERT INTO video_source (video_id, source_id) VALUES ('collab', 1)",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (1, 'archive', 'succeeded', 'collab', 1), (2, 'archive', 'succeeded', 'elsewhere', 1)",
    )
    with _api(settings) as client:
        listed = client.get("/api/videos", params={"listed_by": "UC1"}).json()
        uploaded = client.get("/api/videos", params={"channel_id": "UC1"}).json()
        jobs = client.get("/api/jobs", params={"channel_id": "UC1"}).json()
    assert [v["id"] for v in listed["items"]] == ["own", "collab"] and listed["total"] == 2
    # The Videos page filter stays the uploader.
    assert [v["id"] for v in uploaded["items"]] == ["own"]
    assert [j["id"] for j in jobs] == [1]


def test_api_lists_a_source_s_videos_a_playlists_parent_s_and_oldest_first(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO source (id, url, kind, channel_id, parent_id, enabled, poll_interval_minutes)"
        " VALUES (1, 'https://www.youtube.com/@a/playlists', 'playlists', 'UC1', NULL, true, 360),"
        " (2, 'https://www.youtube.com/playlist?list=PL2', 'playlist', 'UC1', 1, true, 360),"
        " (3, 'https://www.youtube.com/playlist?list=PL3', 'playlist', 'UC1', 1, true, 360),"
        " (4, 'https://www.youtube.com/@a/videos', 'tab', 'UC1', NULL, true, 360)",
        "INSERT INTO video (id, channel_id, type, title, state, upload_date) VALUES"
        " ('p2', 'UC1', 'videos', 'in PL2', 'complete', '2026-01-03'),"
        " ('p3', 'UC1', 'videos', 'in PL3', 'complete', '2026-01-02'),"
        " ('tab', 'UC1', 'videos', 'tab only', 'complete', '2026-01-01')",
        "INSERT INTO video_source (video_id, source_id) VALUES"
        " ('p2', 2), ('p3', 3), ('p2', 4), ('tab', 4)",
    )
    with _api(settings) as client:
        one = client.get("/api/videos", params={"source_id": 2}).json()
        parent = client.get("/api/videos", params={"source_id": 1}).json()
        tab = client.get("/api/videos", params={"source_id": 4, "q": "tab"}).json()
        oldest = client.get("/api/videos", params={"source_id": 1, "order": "oldest"}).json()
    assert [v["id"] for v in one["items"]] == ["p2"] and one["total"] == 1
    assert [v["id"] for v in parent["items"]] == ["p2", "p3"] and parent["total"] == 2
    assert [v["id"] for v in tab["items"]] == ["tab"]
    assert [v["id"] for v in oldest["items"]] == ["p3", "p2"]


@pytest.mark.anyio
async def test_a_failed_live_check_is_one_warning_with_the_channel_name(settings, caplog):
    import logging
    import urllib.error

    from savetube.live import check_logged

    _watch(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    def gone(channel_id):
        raise urllib.error.HTTPError("https://www.youtube.com/feeds", 404, "Not Found", {}, None)

    def broken(channel_id):
        raise ValueError("feed parser broke")

    checks = {"probe": lambda v: UPCOMING, "probe_live": _not_live, "list_streams": _no_streams}
    with caplog.at_level(logging.INFO, logger="savetube"):
        await check_logged(sessionmaker, NASA, fetch_feed=gone, seen={}, **checks)
        await check_logged(sessionmaker, NASA, fetch_feed=broken, seen={}, **checks)
    await engine.dispose()
    expected, unexpected = [r for r in caplog.records if r.getMessage() == "live.check_failed"]
    assert expected.levelno == logging.WARNING and expected.exc_info is None
    assert _fields(expected) == {
        "channel": "NASA",
        "reason": "HTTPError: HTTP Error 404: Not Found",
    }
    assert unexpected.levelno == logging.ERROR and unexpected.exc_info is not None
    assert _fields(unexpected)["channel"] == "NASA"


def test_api_channel_counts_use_the_channel_page_rule(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]'),"
        " ('UC2', 'B', 'B [UC2]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@a/videos', 'tab', 'UC1', true, 360)",
        "INSERT INTO video (id, channel_id, type, title, state, size_bytes) VALUES"
        " ('own', 'UC1', 'videos', 'own', 'complete', 100),"
        " ('collab', 'UC2', 'videos', 'collab', 'complete', 20),"
        " ('pending', 'UC2', 'videos', 'p', 'pending', NULL),"
        " ('elsewhere', 'UC2', 'videos', 'b only', 'complete', 3)",
        "INSERT INTO video_source (video_id, source_id) VALUES ('collab', 1), ('pending', 1)",
    )
    with _api(settings) as client:
        listed = client.get("/api/channels").json()
        counts = {c["id"]: (c["video_count"], c["bytes"]) for c in listed}
        patched = client.patch("/api/channels/UC1", json={"watch_lives": False}).json()
    assert counts == {"UC1": (2, 120), "UC2": (2, 23)}
    assert (patched["video_count"], patched["bytes"]) == (2, 120)


def test_api_channel_statistics_follow_the_channel_list_rule(settings):
    # UC2 uploaded a video UC1's playlist lists and another one no source lists.
    # No source names UC2.
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]'),"
        " ('UC2', 'B', 'B [UC2]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " other_channels) VALUES"
        " (1, 'https://www.youtube.com/playlist?list=PLa', 'playlist', 'UC1', true, 360, true),"
        " (2, 'https://www.youtube.com/@a/videos', 'tab', 'UC1', true, 360, false)",
        "INSERT INTO video (id, channel_id, type, title, state, size_bytes) VALUES"
        " ('own', 'UC1', 'videos', 'own', 'complete', 100),"
        " ('guest', 'UC2', 'videos', 'guest', 'complete', 20),"
        " ('pending', 'UC2', 'videos', 'p', 'pending', 7),"
        " ('elsewhere', 'UC2', 'videos', 'b only', 'complete', 3)",
        "INSERT INTO video_source (video_id, source_id) VALUES"
        " ('own', 2), ('guest', 1), ('guest', 2), ('pending', 1)",
    )
    with _api(settings) as client:
        summary = client.get("/api/stats/summary").json()
    # Two sources listing one video count it once.
    assert summary["by_channel"] == [{"channel_id": "UC1", "name": "A", "bytes": 120, "count": 2}]
    assert (summary["total_bytes"], summary["video_count"]) == (123, 3)


def test_api_counts_jobs_by_state(settings):
    _job_rows(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC2', 'B', 'B [UC2]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v2', 'UC2', 'videos', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1), (3, 'archive', 'queued', 'busy2', 0),"
        " (4, 'archive', 'queued', 'v2', 0), (5, 'record', 'queued', 'busy2', 0)",
    )
    with _api(settings) as client:
        archive = client.get("/api/jobs/counts", params={"kind": "archive"}).json()
        of_uc1 = client.get(
            "/api/jobs/counts", params={"kind": "archive", "channel_id": "UC1"}
        ).json()
        every = client.get("/api/jobs/counts").json()
    assert archive == {
        "queued": 3,
        "running": 1,
        "succeeded": 0,
        "failed": 0,
        "missed": 0,
        "cancelled": 0,
        "interrupted": 0,
    }
    assert (of_uc1["queued"], of_uc1["running"]) == (2, 1)
    assert every["queued"] == 4


def test_api_marks_a_channel_tracked_only_when_a_source_names_it(settings):
    # UC2 is stored only as the uploader of a video UC1's playlist lists.
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]'),"
        " ('UC2', 'B', 'B [UC2]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " other_channels) VALUES"
        " (1, 'https://www.youtube.com/playlist?list=PLa', 'playlist', 'UC1', true, 360, true),"
        " (2, 'https://www.youtube.com/@a/videos', 'tab', 'UC1', false, 360, false)",
        "INSERT INTO video (id, channel_id, type, title, state, size_bytes) VALUES"
        " ('guest', 'UC2', 'videos', 'guest', 'complete', 5)",
        "INSERT INTO video_source (video_id, source_id) VALUES ('guest', 1)",
    )
    with _api(settings) as client:
        listed = client.get("/api/channels").json()
        patched = client.patch("/api/channels/UC2", json={"watch_lives": False}).json()
    # Every channel stays listed for the Videos page filter. Two sources do not double counts.
    assert [(c["id"], c["tracked"], c["video_count"]) for c in listed] == [
        ("UC1", True, 1),
        ("UC2", False, 1),
    ]
    assert patched["tracked"] is False


class _Steps:
    """A runner that plays one step per call and records each call's params."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def __call__(self, url, params, report):
        self.calls.append(params)
        return await self.steps.pop(0)(params, report)


def _home(params):
    return Path(params["paths"]["home"])


def _part(params, name="v1.f251.webm.part", size=2048):
    tmp = _home(params) / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / name).write_bytes(b"x" * size)


AUDIO_INFO = {
    "id": "v1",
    "title": "t",
    "channel_id": "UC1",
    "channel": "A",
    "upload_date": "20260315",
    "duration": 60,
    "format_id": "251",
    "vcodec": "none",
    "acodec": "opus",
}


def _finished(stream_size=5, expected=5):
    """A step that downloads the whole audio file and reports its stream."""

    async def step(params, report):
        home = _home(params)
        home.mkdir(parents=True, exist_ok=True)
        media = home / "v1.webm"
        media.write_bytes(b"m" * stream_size)
        await report({"event": "format", "format_id": "251"})
        await report(
            {"event": "stream", "format_id": "251", "size": stream_size, "expected": expected}
        )
        return {**AUDIO_INFO, "requested_downloads": [{"filepath": str(media)}]}

    return step


def _failing(message):
    async def step(params, report):
        from savetube.jobs import DownloadFailed

        raise DownloadFailed(message)

    return step


class _FakeTools:
    """ffprobe that finds the expected audio stream, ffmpeg with no errors."""

    def __init__(self):
        self.packet_runs = 0

    def probe(self, path):
        return {"format": {"duration": "60.0"}, "streams": [{"codec_type": "audio"}]}

    def packets(self, path):
        self.packet_runs += 1
        return ""


async def _run_steps(settings, runner, tools=None):
    from savetube.jobs import run

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    try:
        await run(
            1,
            settings,
            live=False,
            runner=runner,
            sessionmaker=sessionmaker,
            tools=tools or _FakeTools(),
        )
    except Exception as e:
        if type(e).__name__ != "DownloadFailed":
            raise
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
        video = await session.get(Video, "v1")
    await engine.dispose()
    return jobs, video


def _events(caplog, name):
    return [r for r in caplog.records if r.getMessage() == name]


@pytest.mark.anyio
async def test_the_job_row_names_the_step_after_the_last_byte(settings):
    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    seen = []

    class Tools(_FakeTools):
        def probe(self, path):
            # The checks and the audio copy both start with a probe.
            [(progress,)] = _sql_rows(settings, "SELECT progress FROM job WHERE id = 1")
            seen.append(progress)
            return super().probe(path)

    jobs, _ = await _run_steps(settings, _Steps(_finished()), tools=Tools())

    assert jobs[1].state is JobState.SUCCEEDED
    assert seen == [{"step": "verifying"}, {"step": "audio"}]


@pytest.mark.anyio
async def test_a_worker_shutdown_interrupts_the_download_and_keeps_its_parts(settings, caplog):
    import logging

    from savetube.jobs import run

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    started = asyncio.Event()

    async def downloading(params, report):
        _part(params)
        await report({"event": "format", "format_id": "251"})
        started.set()
        await asyncio.Event().wait()

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    with caplog.at_level(logging.INFO, logger="savetube"):
        task = asyncio.create_task(
            run(1, settings, live=False, runner=_Steps(downloading), sessionmaker=sessionmaker)
        )
        await asyncio.wait_for(started.wait(), 10)
        # procrastinate cancels the task when the worker shuts down.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
        video = await session.get(Video, "v1")
    await engine.dispose()

    [history] = [j for j in jobs.values() if j.id != 1]
    assert (history.state, history.video_id) == ("interrupted", "v1")
    assert history.log_tail == "worker shut down during the download"
    assert history.attempts == 1 and history.finished_at is not None
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert jobs[1].started_at is None and jobs[1].progress is None
    assert video.state is VideoState.PENDING and video.resume_format == "251"
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048
    [line] = _events(caplog, "job.interrupted")
    assert line.levelno == logging.INFO
    assert (_fields(line)["job"], _fields(line)["video"]) == (1, "v1")


@pytest.mark.anyio
@pytest.mark.parametrize("reported", [True, False])
async def test_an_interruption_clears_progress_to_sql_null(settings, reported):
    from savetube.jobs import run

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    started = asyncio.Event()

    async def downloading(params, report):
        if reported:
            await report({"percent": 50})
        started.set()
        await asyncio.Event().wait()

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    task = asyncio.create_task(
        run(1, settings, live=False, runner=_Steps(downloading), sessionmaker=sessionmaker)
    )
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await engine.dispose()
    # A JSON null is IS NOT NULL in SQL, so the hourly cleanup would count it.
    rows = _sql_rows(settings, "SELECT id, progress FROM job WHERE progress IS NOT NULL")
    assert rows == ([(201, {"percent": 50})] if reported else [])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("chat", "langs"),
    [
        # yt-dlp writes a subtitle to the temp path and renames a finished chat
        # from its `.part`, so a file under its final name is complete.
        (".tmp/v1.live_chat.json", ["all", "-live_chat"]),
        ("v1.live_chat.json", ["all", "-live_chat"]),
        (".tmp/v1.live_chat.json.part", ["all"]),
        (None, ["all"]),
    ],
)
async def test_a_resumed_run_skips_a_finished_chat_replay(settings, chat, langs):
    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    workdir = settings.incoming_root / "v1"
    if chat is not None:
        (workdir / chat).parent.mkdir(parents=True, exist_ok=True)
        (workdir / chat).write_text("{}")
    runner = _Steps(_finished())
    await _run_steps(settings, runner)
    [params] = runner.calls
    assert params["subtitleslangs"] == langs


@pytest.mark.anyio
async def test_a_retry_resumes_the_parts_with_the_pinned_format_and_counts_no_attempt(
    settings, caplog
):
    import logging

    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa'",
        "UPDATE video SET resume_format = '251' WHERE id = 'v1'",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (8, 'archive', 'interrupted', 'v1', 1), (9, 'archive', 'interrupted', 'v1', 1)",
    )
    _part({"paths": {"home": str(settings.incoming_root / "v1")}})
    tools = _FakeTools()
    runner = _Steps(_finished())
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner, tools)
    [params] = runner.calls
    assert params["format"] == "251" and params["continuedl"] is True
    assert _home(params) == settings.incoming_root / "v1"
    # Interrupted runs are not failed attempts.
    assert _fields(_events(caplog, "job.start")[0])["attempt"] == "1/3"
    [resume] = _events(caplog, "download.resume")
    assert resume.levelno == logging.INFO
    assert _fields(resume)["present"] == "2KiB" and _fields(resume)["format"] == "251"
    assert jobs[1].state is JobState.SUCCEEDED
    # A resumed download gets the packet pass.
    assert tools.packet_runs == 1
    assert video.verified_checks == ["size", "probe", "packets"]
    assert video.resume_format is None
    assert not (settings.incoming_root / "v1").exists()


@pytest.mark.anyio
async def test_a_pinned_format_no_longer_offered_starts_over_and_a_real_failure_clears_the_parts(
    settings, caplog
):
    import logging

    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa'",
        "UPDATE video SET resume_format = '251' WHERE id = 'v1'",
    )
    old = {"paths": {"home": str(settings.incoming_root / "v1")}}
    _part(old)
    seen = []

    async def gone_at_retry(params, report):
        from savetube.jobs import DownloadFailed

        seen.append(sorted(p.name for p in _home(params).rglob("*")))
        raise DownloadFailed("HTTP Error 403: Forbidden")

    runner = _Steps(
        _failing(
            "DownloadError: ERROR: [youtube] v1: Requested format is not available."
            " Use --list-formats for a list of available formats"
        ),
        gone_at_retry,
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, runner)
    assert [p["format"] for p in runner.calls] == ["251", "wa"]
    # The restart downloads into an empty directory.
    assert seen == [[]]
    [restart] = _events(caplog, "download.restart")
    assert restart.levelno == logging.WARNING and _fields(restart)["format"] == "251"
    assert jobs[1].state is JobState.FAILED and jobs[1].log_tail == "HTTP Error 403: Forbidden"
    assert video.resume_format is None
    assert not (settings.incoming_root / "v1").exists()


@pytest.mark.anyio
async def test_a_fresh_download_is_verified_without_the_packet_pass(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    tools = _FakeTools()
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(_finished()), tools)
    assert jobs[1].state is JobState.SUCCEEDED and video.state is VideoState.COMPLETE
    assert tools.packet_runs == 0
    assert video.verified_checks == ["size", "probe"] and video.verified_at is not None
    assert _events(caplog, "download.resume") == []
    [verified] = _events(caplog, "download.verified")
    assert verified.levelno == logging.INFO and _fields(verified)["checks"] == "size,probe"
    with _api(settings) as client:
        detail = client.get("/api/videos/v1").json()
    assert detail["verified_checks"] == ["size", "probe"] and detail["verified_at"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize("second_ok", [True, False])
async def test_an_integrity_failure_downloads_again_from_zero_once(settings, caplog, second_ok):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    seen = []

    def short_stream():
        inner = _finished(stream_size=4, expected=5)

        async def step(params, report):
            seen.append(sorted(p.name for p in _home(params).rglob("*")))
            return await inner(params, report)

        return step

    second = _finished() if second_ok else short_stream()
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(short_stream(), second))
    [first, *rest] = _events(caplog, "download.integrity_failed")
    assert first.levelno == logging.WARNING
    assert _fields(first)["check"] == "size"
    assert (_fields(first)["size"], _fields(first)["expected"]) == (4, 5)
    if second_ok:
        assert jobs[1].state is JobState.SUCCEEDED and rest == []
    else:
        # The second run started from an empty directory too.
        assert seen == [[], []]
        assert jobs[1].state is JobState.FAILED
        assert jobs[1].log_tail.startswith("integrity check failed: size")
        assert not (settings.incoming_root / "v1").exists()
    # One job row, one attempt, either way.
    assert list(jobs) == [1] and jobs[1].attempts == 1


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


def _sql_rows(settings, statement):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        rows = c.execute(sa.text(statement)).all()
    engine.dispose()
    return rows


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
    assert _fields(done) == {"progress": 4, "queue_jobs": 0, "interrupted": 0}

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


BOT_CHECK = (
    "DownloadError: ERROR: [youtube] v1: Sign in to confirm you’re not a bot."
    " Use --cookies-from-browser or --cookies for the authentication."
)


def _block_state(settings):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        row = c.execute(
            sa.text("SELECT block_count, blocked_until FROM download_settings WHERE id = 1")
        ).one()
    engine.dispose()
    return row.block_count, row.blocked_until


async def _blocked_step(params, report):
    from savetube.jobs import DownloadFailed

    _part(params)
    await report({"event": "format", "format_id": "251"})
    raise DownloadFailed(BOT_CHECK)


@pytest.mark.anyio
async def test_a_youtube_block_interrupts_the_archive_job_and_pauses_downloads(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    before = datetime.now(UTC)
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(_blocked_step))
    [history] = [j for j in jobs.values() if j.id != 1]
    assert (history.state, history.video_id) == ("interrupted", "v1")
    assert history.log_tail == f"blocked by YouTube: {BOT_CHECK}"
    # No attempt counts, and the parts and the pin stay for the resume.
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert video.state is VideoState.PENDING and video.resume_format == "251"
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048
    count, until = _block_state(settings)
    assert count == 1
    assert before + timedelta(minutes=30) <= until <= datetime.now(UTC) + timedelta(minutes=30)
    assert _events(caplog, "job.failed") == []
    [line] = _events(caplog, "job.interrupted")
    assert line.levelno == logging.WARNING and _fields(line)["reason"] == "blocked by YouTube"
    fields = _fields(line)
    assert list(fields) == ["job", "video", "channel", "title", "took", "present", "reason"]
    assert fields["present"] == "2KiB"
    [blocked] = _events(caplog, "dispatch.blocked")
    assert blocked.levelno == logging.WARNING
    fields = _fields(blocked)
    assert (fields["count"], fields["backoff"], fields["job"], fields["video"]) == (
        1,
        "30m0s",
        1,
        "v1",
    )


@pytest.mark.anyio
async def test_a_block_while_paused_interrupts_without_lengthening_the_pause(settings, caplog):
    import logging

    _job_rows(settings, "SELECT setval('job_id_seq', 200)")

    async def blocked_together(params, report):
        from savetube.jobs import DownloadFailed

        # Another job hit the block while this one was downloading.
        _sql(
            settings,
            "UPDATE download_settings SET block_count = 1,"
            " blocked_until = now() + interval '30 minutes'",
        )
        raise DownloadFailed("ERROR: unable to download video data: HTTP Error 429")

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _Steps(blocked_together))
    assert jobs[1].state is JobState.QUEUED and jobs[1].attempts == 0
    count, until = _block_state(settings)
    assert count == 1 and until < datetime.now(UTC) + timedelta(minutes=30)
    assert _events(caplog, "dispatch.blocked") == []
    assert len(_events(caplog, "job.interrupted")) == 1


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


@pytest.mark.anyio
async def test_dispatch_sends_nothing_while_blocked_then_one_probe(settings):
    _waiting_rows(
        settings,
        "UPDATE job SET state = 'succeeded' WHERE id = 10",
        "UPDATE download_settings SET max_concurrent = 5, block_count = 2,"
        " blocked_until = '2026-10-21T03:00:00Z'",
    )
    until = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
    enqueued, jobs = await _dispatch(settings, until - timedelta(minutes=1))
    assert enqueued == []
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [until, until, until]

    # The pause is over, but until a download succeeds only one runs at a time.
    enqueued, jobs = await _dispatch(settings, until)
    assert enqueued == [12]
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [None, None, None]


@pytest.mark.anyio
async def test_dispatch_while_blocked_waits_for_the_later_of_pause_and_window(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5, block_count = 1,"
        " window_start = '23:00', window_end = '05:00', window_enabled = true,"
        " blocked_until = '2026-10-22T00:30:00+09'",
    )
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, noon)
    assert enqueued == []
    assert jobs[12].deferred_until == datetime(2026, 10, 22, 0, 30, tzinfo=TIMEZONE)


@pytest.mark.anyio
async def test_a_worker_hands_back_a_job_while_blocked(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE download_settings SET blocked_until = now() + interval '1 hour'")
    with caplog.at_level(logging.INFO, logger="savetube"):
        job, runner, _ = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.queue_job_id is None
    [line] = _events(caplog, "job.returned")
    assert _fields(line)["reason"] == "blocked"


@pytest.mark.anyio
async def test_the_probe_admits_one_archive_job_at_a_time(settings, caplog):
    import logging

    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1)",
        "UPDATE download_settings SET max_concurrent = 5, block_count = 1,"
        " blocked_until = now() - interval '1 minute'",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        job, runner, _ = await _run(settings)
    assert runner.params is None and job.state is JobState.QUEUED
    assert _fields(_events(caplog, "job.returned")[0])["reason"] == "slots"


@pytest.mark.anyio
async def test_a_success_after_the_pause_ends_the_probe(settings, caplog):
    import logging

    _job_rows(
        settings,
        "UPDATE channel SET format = 'wa'",
        "UPDATE download_settings SET block_count = 2, blocked_until = now() - interval '1 minute'",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, _ = await _run_steps(settings, _Steps(_finished()))
    assert jobs[1].state is JobState.SUCCEEDED
    assert _block_state(settings) == (0, None)
    [line] = _events(caplog, "dispatch.unblocked")
    assert line.levelno == logging.INFO and _fields(line)["count"] == 2


@pytest.mark.anyio
async def test_a_blocked_recording_still_fails(settings, caplog):
    import logging

    from savetube.jobs import DownloadFailed, run

    _job_rows(settings, "UPDATE job SET kind = 'record'")

    async def blocked(params, report):
        raise DownloadFailed(BOT_CHECK)

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    with caplog.at_level(logging.INFO, logger="savetube"), pytest.raises(DownloadFailed):
        await run(1, settings, live=True, runner=_Steps(blocked), sessionmaker=sessionmaker)
    async with sessionmaker() as session:
        job = await session.get(Job, 1)
    await engine.dispose()
    assert job.state is JobState.FAILED and job.log_tail == BOT_CHECK
    assert _block_state(settings) == (0, None)
    assert len(_events(caplog, "job.failed")) == 1


def test_migration_0009_gives_back_attempts_a_bot_check_took(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0008")
    _job_rows(
        settings,
        "UPDATE job SET state = 'succeeded' WHERE id = 1",
        "INSERT INTO job (id, kind, state, video_id, attempts, log_tail) VALUES"
        " (2, 'archive', 'failed', 'v1', 1, 'Sign in to confirm you’re not a bot.'),"
        " (3, 'archive', 'failed', 'v1', 1, 'HTTP Error 403: Forbidden'),"
        " (4, 'record', 'failed', 'busy2', 1, 'Sign in to confirm you’re NOT A BOT.')",
    )
    command.upgrade(cfg, "head")
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        states = dict(c.execute(sa.text("SELECT id, state FROM job")).all())
    assert states == {1: "succeeded", 2: "interrupted", 3: "failed", 4: "failed"}
    assert _block_state(settings) == (0, None)

    command.downgrade(cfg, "0008")
    with engine.connect() as c:
        states = dict(c.execute(sa.text("SELECT id, state FROM job")).all())
        columns = set(
            c.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'download_settings'"
                )
            ).scalars()
        )
    engine.dispose()
    # The downgrade leaves the data as it is.
    assert states[2] == "interrupted"
    assert not columns & {"blocked_until", "block_count"}


def test_migration_0012_sets_failed_videos_with_a_queued_archive_job_pending(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0011")
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('waits', 'UC1', 'videos', 't', 'failed'), ('gave_up', 'UC1', 'videos', 't', 'failed'),"
        " ('recorded', 'UC1', 'lives', 't', 'failed'), ('done', 'UC1', 'videos', 't', 'complete')",
        "INSERT INTO job (kind, state, video_id, attempts) VALUES"
        " ('archive', 'failed', 'waits', 1), ('archive', 'queued', 'waits', 0),"
        " ('archive', 'failed', 'gave_up', 1),"
        " ('record', 'queued', 'recorded', 0),"
        " ('archive', 'queued', 'done', 0)",
    )
    command.upgrade(cfg, "head")
    states = dict(_sql_rows(settings, "SELECT id, state FROM video"))
    assert states == {
        "waits": "pending",
        "gave_up": "failed",
        "recorded": "failed",
        "done": "complete",
    }
    command.downgrade(cfg, "0011")
    assert dict(_sql_rows(settings, "SELECT id, state FROM video"))["waits"] == "pending"


@pytest.mark.anyio
async def test_dispatch_sends_nothing_while_paused_and_leaves_no_deferral(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE job SET state = 'succeeded' WHERE id = 10",
        "UPDATE download_settings SET max_concurrent = 5, paused = true,"
        " window_start = '23:00', window_end = '05:00', window_enabled = true",
    )
    # Outside the window too, but a pause has no known end.
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, noon)
    assert enqueued == []
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [None, None, None]
    opens = datetime(2026, 10, 21, 23, 0, tzinfo=TIMEZONE)
    enqueued, _ = await _dispatch(settings, opens)
    assert enqueued == []

    _sql(settings, "UPDATE download_settings SET paused = false")
    enqueued, _ = await _dispatch(settings, opens)
    assert enqueued == [12, 13, 14]


@pytest.mark.anyio
async def test_a_worker_hands_back_a_job_while_paused(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE download_settings SET paused = true")
    with caplog.at_level(logging.INFO, logger="savetube"):
        job, runner, _ = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.queue_job_id is None and job.attempts == 0
    assert job.deferred_until is None
    [line] = _events(caplog, "job.returned")
    assert _fields(line)["reason"] == "paused"


@pytest.mark.anyio
async def test_a_pause_interrupts_a_running_archive_download_and_keeps_its_parts(
    settings, caplog, monkeypatch
):
    import logging

    from savetube import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "PAUSE_POLL_SECONDS", 0.05)
    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")

    async def downloading(params, report):
        _part(params)
        await report({"event": "format", "format_id": "251"})
        _sql(settings, "UPDATE download_settings SET paused = true")
        await asyncio.Event().wait()

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(downloading)), 10)

    [history] = [j for j in jobs.values() if j.id != 1]
    assert (history.state, history.video_id) == ("interrupted", "v1")
    assert history.log_tail == "paused by user"
    assert history.attempts == 1 and history.finished_at is not None
    assert (jobs[1].state, jobs[1].queue_job_id, jobs[1].attempts) == (JobState.QUEUED, None, 0)
    assert jobs[1].deferred_until is None
    assert video.state is VideoState.PENDING and video.resume_format == "251"
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048
    assert _events(caplog, "job.failed") == []
    [line] = _events(caplog, "job.interrupted")
    assert line.levelno == logging.INFO and _fields(line)["reason"] == "paused by user"


SECOND_RUN_START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


async def _reclaim_and_start_again(settings):
    """The scheduler reclaims job 1 from a live worker, and a second run of it
    starts, pins its own format, and works in the same directory."""
    from savetube.jobs import reclaim_stalled

    async def stalled():
        return [(500, 1)]

    async def finish(queue_job_id):
        pass

    engine = make_engine(settings.sqlalchemy_url)
    await reclaim_stalled(make_sessionmaker(engine), settings, stalled=stalled, finish=finish)
    await engine.dispose()
    _sql(
        settings,
        "UPDATE job SET state = 'running', attempts = 1, queue_job_id = 501,"
        f" started_at = '{SECOND_RUN_START.isoformat()}' WHERE id = 1",
        "UPDATE video SET state = 'downloading', resume_format = '140' WHERE id = 'v1'",
    )


def _assert_left_to_the_second_run(settings, jobs, video, caplog):
    import logging

    # Only the reclaim wrote a history row.
    [history] = [j for j in jobs.values() if j.id != 1]
    assert history.log_tail == "worker stopped during the download"
    owner = jobs[1]
    assert (owner.state, owner.queue_job_id, owner.attempts) == (JobState.RUNNING, 501, 1)
    assert owner.started_at == SECOND_RUN_START and owner.finished_at is None
    assert (video.state, video.resume_format) == (VideoState.DOWNLOADING, "140")
    assert video.dir_path is None
    assert (settings.incoming_root / "v1").is_dir()
    [line] = _events(caplog, "job.disowned")
    fields = _fields(line)
    assert line.levelno == logging.WARNING
    assert (fields["job"], fields["video"]) == (1, "v1")
    assert fields["reason"]
    assert _events(caplog, "job.failed") == []
    assert _events(caplog, "job.done") == []
    assert [_fields(r)["reason"] for r in _events(caplog, "job.interrupted")] == [
        "worker stopped during the download"
    ]


@pytest.mark.anyio
async def test_a_run_whose_job_was_reclaimed_stops_and_leaves_the_directory(
    settings, caplog, monkeypatch
):
    import logging

    from savetube import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "PAUSE_POLL_SECONDS", 0.05)
    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    cancelled = asyncio.Event()

    async def downloading(params, report):
        _part(params)
        await report({"event": "format", "format_id": "251"})
        await _reclaim_and_start_again(settings)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(downloading)), 10)

    assert cancelled.is_set()
    _assert_left_to_the_second_run(settings, jobs, video, caplog)
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048


@pytest.mark.anyio
async def test_a_disowned_run_that_finishes_moves_nothing_into_the_tree(settings, caplog):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    finished = _finished()

    async def done_after_reclaim(params, report):
        info = await finished(params, report)
        await _reclaim_and_start_again(settings)
        return info

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(done_after_reclaim)), 10)

    _assert_left_to_the_second_run(settings, jobs, video, caplog)
    assert (settings.incoming_root / "v1" / "v1.webm").read_bytes() == b"mmmmm"
    assert not (settings.archive_root / "A [UC1]").exists()


@pytest.mark.anyio
async def test_a_disowned_run_that_fails_writes_nothing(settings, caplog):
    import logging

    from savetube.jobs import DownloadFailed

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")

    async def fails_after_reclaim(params, report):
        _part(params)
        await _reclaim_and_start_again(settings)
        raise DownloadFailed("ERROR: something broke")

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(fails_after_reclaim)), 10)

    _assert_left_to_the_second_run(settings, jobs, video, caplog)
    assert (settings.incoming_root / "v1" / ".tmp" / "v1.f251.webm.part").stat().st_size == 2048


@pytest.mark.anyio
async def test_a_disowned_run_reports_nothing_onto_the_new_owners_row(settings, caplog):
    import logging

    from savetube.jobs import DownloadFailed

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")

    async def reports_after_reclaim(params, report):
        _part(params)
        await _reclaim_and_start_again(settings)
        _sql(settings, """UPDATE job SET progress = '{"percent": 50.0}' WHERE id = 1""")
        await report({"percent": 10.0})
        await report({"event": "format", "format_id": "251"})
        raise DownloadFailed("ERROR: something broke")

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await asyncio.wait_for(
            _run_steps(settings, _Steps(reports_after_reclaim)), 10
        )

    _assert_left_to_the_second_run(settings, jobs, video, caplog)
    assert jobs[1].progress == {"percent": 50.0}


@pytest.mark.anyio
async def test_reclaim_takes_a_worker_as_stalled_after_300_s_without_a_heartbeat(
    settings, monkeypatch
):
    from savetube import scheduler
    from savetube.queue import app

    asked = []

    async def get_stalled_jobs(**kwargs):
        asked.append(kwargs)
        return []

    engine = make_engine(settings.sqlalchemy_url)
    monkeypatch.setattr(scheduler, "default_sessionmaker", lambda: make_sessionmaker(engine))
    monkeypatch.setattr(app.job_manager, "get_stalled_jobs", get_stalled_jobs)
    await scheduler.reclaim(settings)
    await engine.dispose()
    [kwargs] = asked
    assert kwargs["seconds_since_heartbeat"] == 300
    assert (kwargs["queue"], kwargs["task_name"]) == ("archive", "archive")


@pytest.mark.anyio
async def test_a_pause_never_cancels_a_live_recording(settings, monkeypatch):
    from savetube import jobs as jobs_module
    from savetube.jobs import DownloadFailed, run

    monkeypatch.setattr(jobs_module, "PAUSE_POLL_SECONDS", 0.01)
    _job_rows(
        settings,
        "UPDATE job SET kind = 'record'",
        "UPDATE download_settings SET paused = true",
    )

    async def recording(params, report):
        # Many poll intervals pass while the recording runs.
        await asyncio.sleep(0.3)
        raise DownloadFailed("the live ended badly")

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    with pytest.raises(DownloadFailed):
        await run(1, settings, live=True, runner=_Steps(recording), sessionmaker=sessionmaker)
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
    await engine.dispose()
    assert list(jobs) == [1]
    assert jobs[1].state is JobState.FAILED and jobs[1].log_tail == "the live ended badly"


def test_api_pauses_and_resumes_archive_downloads(settings, caplog):
    import logging

    _job_rows(settings)
    with _api(settings) as client, caplog.at_level(logging.INFO, logger="savetube"):
        assert client.get("/api/download-settings").json()["paused"] is False
        put = client.put("/api/download-settings/paused", json={"paused": True})
        assert put.status_code == 200 and put.json()["paused"] is True
        assert client.get("/api/download-settings").json()["paused"] is True
        # Saving the limits leaves the pause as it is.
        limits = {"max_concurrent": 2, "cookie_profile": None}
        assert client.put("/api/download-settings", json=limits).json()["paused"] is True
        assert client.put("/api/download-settings/paused", json={}).status_code == 422
        put = client.put("/api/download-settings/paused", json={"paused": False})
        assert put.json()["paused"] is False
    [paused] = _events(caplog, "dispatch.paused")
    [resumed] = _events(caplog, "dispatch.resumed")
    assert paused.levelno == resumed.levelno == logging.INFO


async def _work(settings, monkeypatch, step, *, reclaim):
    """A real worker takes job 1's queue job. With `reclaim`, the run first
    plays the scheduler's reclaim, which fails that queue job, and a second
    run's start. Returns the queue job's id."""
    from procrastinate import PsycopgConnector
    from procrastinate.jobs import Status

    from savetube import jobs as jobs_module
    from savetube.db import PSYCOPG_CONNECT_ARGS
    from savetube.queue import ARCHIVE_QUEUE, app, archive

    real_run = jobs_module.run
    engine = make_engine(settings.sqlalchemy_url)

    async def first(params, report):
        if reclaim:
            await app.job_manager.finish_job_by_id_async(
                queue_job_id, status=Status.FAILED, delete_job=False
            )
            await _reclaim_and_start_again(settings)
        return await step(params, report)

    async def run(job_id, _settings, *, live, runner):
        await real_run(
            job_id,
            settings,
            live=live,
            runner=_Steps(first),
            sessionmaker=make_sessionmaker(engine),
            tools=_FakeTools(),
        )

    monkeypatch.setattr(jobs_module, "run", run)
    connector = PsycopgConnector(conninfo=settings.conninfo, kwargs=PSYCOPG_CONNECT_ARGS)
    with app.replace_connector(connector):
        async with app.open_async():
            queue_job_id = await archive.defer_async(job_id=1)
            _sql(settings, f"UPDATE job SET queue_job_id = {queue_job_id} WHERE id = 1")
            # The worker settings of savetube.cli.
            await asyncio.wait_for(
                app.run_worker_async(
                    queues=[ARCHIVE_QUEUE],
                    wait=False,
                    listen_notify=False,
                    delete_jobs="successful",
                    install_signal_handlers=False,
                ),
                10,
            )
    await engine.dispose()
    return queue_job_id


def _errors(caplog):
    import logging

    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["done", "failed"])
async def test_a_worker_ends_a_reclaimed_queue_job_without_an_error(
    settings, caplog, monkeypatch, outcome
):
    import gc
    import logging

    from savetube.jobs import DownloadFailed

    _job_rows(settings, "UPDATE channel SET format = 'wa'", "SELECT setval('job_id_seq', 200)")
    finished = _finished()

    async def step(params, report):
        if outcome == "failed":
            _part(params)
            raise DownloadFailed("ERROR: something broke")
        return await finished(params, report)

    with caplog.at_level(logging.DEBUG):
        queue_job_id = await _work(settings, monkeypatch, step, reclaim=True)
        # An unretrieved task exception logs when the task is collected.
        gc.collect()

    assert _errors(caplog) == []
    assert [_fields(r)["job"] for r in _events(caplog, "job.disowned")] == [1]
    rows = _sql_rows(settings, f"SELECT status FROM procrastinate_jobs WHERE id = {queue_job_id}")
    assert rows == [("failed",)]


@pytest.mark.anyio
async def test_a_worker_still_ends_its_own_queue_job(settings, caplog, monkeypatch):
    import gc
    import logging

    from procrastinate import PsycopgConnector
    from procrastinate.exceptions import ConnectorException
    from procrastinate.jobs import Job as QueueJob
    from procrastinate.jobs import Status

    from savetube.db import PSYCOPG_CONNECT_ARGS
    from savetube.queue import app

    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    with caplog.at_level(logging.DEBUG):
        queue_job_id = await _work(settings, monkeypatch, _finished(), reclaim=False)
        gc.collect()

    assert _errors(caplog) == []
    assert [_fields(r)["job"] for r in _events(caplog, "job.done")] == [1]
    rows = _sql_rows(settings, f"SELECT id FROM procrastinate_jobs WHERE id = {queue_job_id}")
    assert rows == []

    # Only a queue job the reclaim failed counts as already ended.
    ended = _queue_row(settings, "succeeded")
    job = QueueJob(id=ended, queue="archive", lock=None, queueing_lock=None, task_name="archive")
    connector = PsycopgConnector(conninfo=settings.conninfo, kwargs=PSYCOPG_CONNECT_ARGS)
    with app.replace_connector(connector):
        async with app.open_async():
            with pytest.raises(ConnectorException):
                await app.job_manager.finish_job(job, Status.SUCCEEDED, delete_job=True)


def _fake_bin(monkeypatch, tmp_path, *, probe=None, ffmpeg_error=None):
    """ffprobe printing `probe` as JSON, and ffmpeg that writes half a file to
    its output and fails with `ffmpeg_error`, first on PATH."""
    import json

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if probe is not None:
        (bin_dir / "ffprobe").write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(probe)}\nEOF\n")
    if ffmpeg_error is not None:
        (bin_dir / "ffmpeg").write_text(
            '#!/bin/bash\nfor a; do last="$a"; done\nprintf half > "$last"\n'
            f"echo '{ffmpeg_error}' >&2\nexit 1\n"
        )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


MERGED_PROBE = {
    "format": {"duration": "60.0"},
    "streams": [{"codec_type": "video"}, {"codec_type": "audio", "codec_name": "opus"}],
}


@pytest.mark.anyio
async def test_an_archived_video_gets_its_audio_copy_beside_it_in_the_tree(settings):
    from test_audio import make_media, needs_ffmpeg

    from savetube.integrity import Tools

    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg or ffprobe not on PATH")
    _job_rows(settings)

    async def merged(params, report):
        home = _home(params)
        home.mkdir(parents=True, exist_ok=True)
        media = make_media(home / "v1.mkv")
        return {
            **AUDIO_INFO,
            "duration": 1,
            "vcodec": "mp4v",
            "ext": "mkv",
            "requested_downloads": [{"filepath": str(media)}],
        }

    jobs, video = await _run_steps(settings, _Steps(merged), Tools())
    assert jobs[1].state is JobState.SUCCEEDED and video.state is VideoState.COMPLETE
    d = settings.archive_root / video.dir_path
    assert sorted(p.name for p in d.iterdir()) == ["v1.mkv", "v1.opus"]
    # size_bytes counts every file in the directory, the audio copy too.
    assert video.size_bytes == (d / "v1.mkv").stat().st_size + (d / "v1.opus").stat().st_size


@pytest.mark.anyio
async def test_a_failed_audio_copy_logs_a_warning_and_the_video_stays_complete(
    settings, caplog, monkeypatch, tmp_path
):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    _fake_bin(monkeypatch, tmp_path, ffmpeg_error="Invalid data found when processing input")

    class Merged(_FakeTools):
        def probe(self, path):
            return MERGED_PROBE

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(_finished()), Merged())
    assert jobs[1].state is JobState.SUCCEEDED and video.state is VideoState.COMPLETE
    [failed] = _events(caplog, "audio.failed")
    assert failed.levelno == logging.WARNING
    assert "Invalid data found" in _fields(failed)["reason"]
    d = settings.archive_root / video.dir_path
    assert sorted(p.name for p in d.iterdir()) == ["v1.webm"]


def _stored_videos(settings, media=None):
    """a1 lacks its audio copy, b2 has one, c3 lost its video file, d4 is not
    stored, e5 is a live recording."""
    root = settings.archive_root / "A [UC1]" / "videos"
    for vid, files in [
        ("a1", ["a1.mkv", "a1.info.json"]),
        ("b2", ["b2.mkv", "b2.m4a"]),
        ("c3", ["c3.info.json"]),
        ("e5", ["e5.mkv"]),
    ]:
        (root / vid).mkdir(parents=True)
        for name in files:
            (root / vid / name).write_bytes(b"x" * 10)
    if media is not None:
        media(root / "a1" / "a1.mkv")
    rows = ", ".join(
        f"('{vid}', 'UC1', 'videos', 't', '{state}', 'A [UC1]/videos/{vid}', 20,"
        """ '{"ext": "mkv"}')"""
        for vid, state in [
            ("a1", "complete"),
            ("b2", "complete"),
            ("c3", "complete"),
            ("d4", "pending"),
            ("e5", "complete"),
        ]
    )
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, dir_path, size_bytes, info)"
        f" VALUES {rows}",
        "INSERT INTO job (kind, state, video_id, attempts) VALUES ('record', 'succeeded', 'e5', 1)",
    )
    return root


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.anyio
async def test_the_audio_backfill_dry_run_lists_each_video_and_writes_nothing(
    settings, monkeypatch, tmp_path
):
    from savetube.audio import backfill
    from savetube.integrity import Tools

    root = _stored_videos(settings)
    before = _tree(root)
    _fake_bin(monkeypatch, tmp_path, probe=MERGED_PROBE)
    lines = []
    engine = make_engine(settings.sqlalchemy_url)
    counts = await backfill(
        make_sessionmaker(engine),
        settings.archive_root,
        tools=Tools(),
        dry_run=True,
        out=lines.append,
    )
    await engine.dispose()
    assert lines == [
        "a1 would extract A [UC1]/videos/a1/a1.opus",
        "b2 skipped: has b2.m4a",
        "c3 failed: video file not found",
        "would extract 1, skipped 1, failed 1",
    ]
    assert counts == {"extracted": 1, "skipped": 1, "failed": 1}
    assert _tree(root) == before
    assert _sql_rows(settings, "SELECT DISTINCT size_bytes FROM video") == [(20,)]


def test_the_extract_audio_command_dry_run(settings, monkeypatch, tmp_path, capsys):
    from savetube import cli
    from savetube.config import get_settings

    root = _stored_videos(settings)
    before = _tree(root)
    _fake_bin(monkeypatch, tmp_path, probe=MERGED_PROBE)
    monkeypatch.setenv("SAVETUBE_DATABASE_URL", settings.database_url.get_secret_value())
    monkeypatch.setenv("SAVETUBE_ARCHIVE_ROOT", str(settings.archive_root))
    monkeypatch.setattr("sys.argv", ["savetube", "extract-audio", "--dry-run"])
    # The command's logging setup would replace pytest's handlers.
    monkeypatch.setattr("savetube.logs.configure", lambda level: None)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as e:
            cli.main()
    finally:
        get_settings.cache_clear()
    assert e.value.code == 1
    assert capsys.readouterr().out.splitlines() == [
        "a1 would extract A [UC1]/videos/a1/a1.opus",
        "b2 skipped: has b2.m4a",
        "c3 failed: video file not found",
        "would extract 1, skipped 1, failed 1",
    ]
    assert _tree(root) == before


@pytest.mark.anyio
async def test_the_audio_backfill_writes_missing_copies_once(settings):
    from test_audio import make_media, needs_ffmpeg

    from savetube.audio import backfill
    from savetube.integrity import Tools

    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg or ffprobe not on PATH")
    root = _stored_videos(settings, media=make_media)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    first, second = [], []
    await backfill(sessionmaker, settings.archive_root, tools=Tools(), out=first.append)
    await backfill(sessionmaker, settings.archive_root, tools=Tools(), out=second.append)
    await engine.dispose()
    assert first == [
        "a1 extracted A [UC1]/videos/a1/a1.opus",
        "b2 skipped: has b2.m4a",
        "c3 failed: video file not found",
        "extracted 1, skipped 1, failed 1",
    ]
    assert second[0] == "a1 skipped: has a1.opus"
    assert second[-1] == "extracted 0, skipped 2, failed 1"
    a1 = root / "a1"
    assert sorted(p.name for p in a1.iterdir()) == ["a1.info.json", "a1.mkv", "a1.opus"]
    [(size,)] = _sql_rows(settings, "SELECT size_bytes FROM video WHERE id = 'a1'")
    assert size == sum(p.stat().st_size for p in a1.iterdir())


# The start of today in Seoul, as SQL.
_KST_TODAY = "(date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul')"


@pytest.mark.anyio
async def test_the_daily_limit_hands_back_a_4th_start_at_limit_3(settings, caplog):
    import logging

    _job_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 10, daily_limit = 3",
        # Two successes today and one running download make three.
        "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
        " (2, 'archive', 'succeeded', 'busy1', 1, now()),"
        f" (3, 'archive', 'succeeded', 'busy1', 1, {_KST_TODAY}),"
        " (4, 'archive', 'running', 'busy1', 1, NULL),"
        # None of these count: yesterday, a recording, an interrupted run, a failure.
        f" (5, 'archive', 'succeeded', 'busy1', 1, {_KST_TODAY} - interval '1 second'),"
        " (6, 'record', 'succeeded', 'busy2', 1, now()),"
        " (7, 'archive', 'interrupted', 'busy1', 1, now()),"
        " (8, 'archive', 'failed', 'busy1', 1, now())",
    )
    with caplog.at_level(logging.INFO, logger="savetube"):
        job, runner, queue_rows = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.queue_job_id is None and job.attempts == 0
    assert queue_rows == 0
    [line] = _events(caplog, "job.returned")
    assert _fields(line)["reason"] == "daily_limit"

    _sql(settings, "UPDATE download_settings SET daily_limit = 4")
    job, runner, _ = await _run(settings)
    assert runner.params is not None and job.attempts == 1


@pytest.mark.anyio
async def test_a_download_running_when_the_daily_limit_is_reached_runs_to_the_end(
    settings, monkeypatch
):
    from savetube import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "PAUSE_POLL_SECONDS", 0.05)
    _job_rows(settings, "UPDATE channel SET format = 'wa'")

    async def capped_mid_run(params, report):
        _sql(
            settings,
            "UPDATE download_settings SET daily_limit = 1",
            "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
            " (2, 'archive', 'succeeded', 'busy1', 1, now())",
        )
        await asyncio.sleep(0.3)
        return await _finished()(params, report)

    jobs, video = await asyncio.wait_for(_run_steps(settings, _Steps(capped_mid_run)), 10)
    assert jobs[1].state is JobState.SUCCEEDED
    assert video.state is VideoState.COMPLETE


@pytest.mark.anyio
async def test_dispatch_fills_only_the_slots_the_daily_limit_leaves(settings):
    from savetube.limits import TIMEZONE

    now = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5, daily_limit = 3",
        # Job 10 runs, so one success today leaves one start.
        "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
        " (20, 'archive', 'succeeded', 'a', 1, '2026-10-21T08:00:00+09')",
    )
    enqueued, jobs = await _dispatch(settings, now)
    assert enqueued == [12]
    assert [jobs[i].deferred_until for i in (13, 14)] == [None, None]
    # The dispatched job holds the last start of the day.
    enqueued, _ = await _dispatch(settings, now)
    assert enqueued == []


@pytest.mark.anyio
async def test_the_daily_count_resets_at_midnight_in_seoul(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE job SET state = 'succeeded', finished_at = '2026-10-21T23:30:00+09' WHERE id = 10",
        "UPDATE download_settings SET max_concurrent = 5, daily_limit = 1",
    )
    midnight = datetime(2026, 10, 22, 0, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, midnight - timedelta(minutes=10))
    assert enqueued == []
    assert [jobs[i].deferred_until for i in (12, 13, 14)] == [midnight, midnight, midnight]
    assert jobs[15].deferred_until is None

    enqueued, jobs = await _dispatch(settings, midnight)
    assert enqueued == [12]
    assert [jobs[i].deferred_until for i in (13, 14)] == [None, None]


@pytest.mark.anyio
async def test_the_daily_limit_defers_to_the_later_of_midnight_and_the_window(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5, daily_limit = 1,"
        " window_start = '01:00', window_end = '05:00', window_enabled = true",
    )
    # Job 10 runs, so the cap holds. The window opens after midnight.
    late = datetime(2026, 10, 21, 23, 30, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, late)
    assert enqueued == []
    assert jobs[12].deferred_until == datetime(2026, 10, 22, 1, 0, tzinfo=TIMEZONE)

    # Inside a window that crosses midnight, the cap waits for midnight.
    _sql(settings, "UPDATE download_settings SET window_start = '23:00'")
    enqueued, jobs = await _dispatch(settings, late)
    assert enqueued == []
    assert jobs[12].deferred_until == datetime(2026, 10, 22, 0, 0, tzinfo=TIMEZONE)


@pytest.mark.anyio
async def test_a_disabled_window_keeps_its_times_and_is_ignored(settings):
    from savetube.limits import TIMEZONE

    _waiting_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 5,"
        " window_start = '23:00', window_end = '05:00', window_enabled = false",
    )
    noon = datetime(2026, 10, 21, 12, 0, tzinfo=TIMEZONE)
    enqueued, jobs = await _dispatch(settings, noon)
    assert enqueued == [12, 13, 14]
    assert all(j.deferred_until is None for j in jobs.values())


@pytest.mark.anyio
async def test_a_worker_ignores_a_disabled_window(settings):
    _job_rows(settings, _closed_window_sql(), "UPDATE download_settings SET window_enabled = false")
    job, runner, _ = await _run(settings)
    assert runner.params is not None and job.attempts == 1


def test_api_daily_limit_window_switch_and_todays_count(settings):
    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts, finished_at) VALUES"
        " (2, 'archive', 'succeeded', 'busy1', 1, now()),"
        " (3, 'archive', 'running', 'busy1', 1, NULL),"
        f" (4, 'archive', 'succeeded', 'busy1', 1, {_KST_TODAY} - interval '1 second')",
    )
    body = {
        "max_concurrent": 1,
        "rate_limit_bytes": None,
        "window_start": "23:00",
        "window_end": "05:00",
        "window_enabled": False,
        "daily_limit": 3,
        "cookie_profile": None,
    }
    with _api(settings) as client:
        got = client.get("/api/download-settings").json()
        assert (got["daily_limit"], got["window_enabled"], got["downloaded_today"]) == (
            None,
            False,
            2,
        )
        put = client.put("/api/download-settings", json=body)
        assert put.status_code == 200
        got = client.get("/api/download-settings").json()
        assert {k: got[k] for k in body} == body
        assert got["downloaded_today"] == 2
        on = body | {"window_enabled": True, "daily_limit": None}
        assert client.put("/api/download-settings", json=on).status_code == 200
        got = client.get("/api/download-settings").json()
        assert (got["window_enabled"], got["daily_limit"]) == (True, None)
        for bad in (
            body | {"daily_limit": 0},
            body | {"window_enabled": True, "window_start": None, "window_end": None},
        ):
            assert client.put("/api/download-settings", json=bad).status_code == 422, bad


def test_migration_0013_enables_a_window_that_has_both_times(settings):
    from alembic import command

    from savetube.db.migrate import alembic_config

    cfg = alembic_config(settings.sqlalchemy_url)
    command.downgrade(cfg, "0012")
    _sql(settings, "UPDATE download_settings SET window_start = '23:00', window_end = '05:00'")
    command.upgrade(cfg, "head")
    assert _sql_rows(
        settings, "SELECT window_enabled, window_start, daily_limit FROM download_settings"
    ) == [(True, dt_time(23, 0), None)]

    command.downgrade(cfg, "0012")
    _sql(settings, "UPDATE download_settings SET window_start = NULL, window_end = NULL")
    command.upgrade(cfg, "head")
    assert _sql_rows(settings, "SELECT window_enabled FROM download_settings") == [(False,)]


MEDIA = bytes(range(256)) * 4


def _media_rows(settings, *, state="complete", dir_path="Ch [UC1]/videos/a [m1]"):
    root = settings.archive_root
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Ch', 'Ch [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, dir_path, info) VALUES"
        f" ('m1', 'UC1', 'videos', 'a', '{state}', '{dir_path}', '{{\"ext\": \"mkv\"}}')",
    )
    directory = root / "Ch [UC1]/videos/a [m1]"
    directory.mkdir(parents=True)
    (directory / "m1.mkv").write_bytes(MEDIA)
    (directory / "m1.opus").write_bytes(MEDIA[:100])
    return directory


def test_media_serves_the_whole_file_with_matroska_type(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/media")
    assert r.status_code == 200
    assert r.content == MEDIA
    assert r.headers["content-type"] == "video/x-matroska"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == "1024"


def test_media_serves_a_byte_range(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/media", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == MEDIA[10:20]
    assert r.headers["content-range"] == "bytes 10-19/1024"
    assert r.headers["content-type"] == "video/x-matroska"


def test_media_serves_a_suffix_range(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/media", headers={"Range": "bytes=-24"})
    assert r.status_code == 206
    assert r.content == MEDIA[1000:]
    assert r.headers["content-range"] == "bytes 1000-1023/1024"


def test_media_refuses_a_range_past_the_end(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/media", headers={"Range": "bytes=5000-6000"})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */1024"


def test_media_answers_head_without_a_body(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.head("/api/videos/m1/media")
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-length"] == "1024"
    assert r.headers["content-type"] == "video/x-matroska"


def test_audio_serves_the_opus_copy_as_ogg(settings):
    _media_rows(settings)
    with _api(settings) as client:
        full = client.get("/api/videos/m1/audio")
        part = client.get("/api/videos/m1/audio", headers={"Range": "bytes=0-9"})
    assert full.status_code == 200 and full.content == MEDIA[:100]
    assert full.headers["content-type"] == "audio/ogg"
    assert part.status_code == 206 and part.content == MEDIA[:10]


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_media_is_404_until_the_video_is_complete(settings, state):
    _media_rows(settings, state=state)
    with _api(settings) as client:
        assert client.get("/api/videos/m1/media").status_code == 404
        assert client.get("/api/videos/m1/audio").status_code == 404


def test_media_is_404_for_a_missing_file_or_video(settings):
    directory = _media_rows(settings)
    (directory / "m1.opus").unlink()
    with _api(settings) as client:
        assert client.get("/api/videos/m1/audio").status_code == 404
        assert client.get("/api/videos/nope/media").status_code == 404


def test_media_never_leaves_the_archive_root(settings):
    outside = settings.archive_root.parent / "outside"
    outside.mkdir(parents=True)
    (outside / "m1.mkv").write_bytes(b"secret")
    _media_rows(settings, dir_path="../outside")
    with _api(settings) as client:
        assert client.get("/api/videos/m1/media").status_code == 404


def test_media_needs_a_login(settings):
    _media_rows(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/api/videos/m1/media")
    assert r.status_code == 401


VTT_JA = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nこんにちは\n"


def _subtitle_rows(settings, *, state="complete", dir_path="Ch [UC1]/videos/a [m1]"):
    directory = _media_rows(settings, state=state, dir_path=dir_path)
    info = json.dumps({"ext": "mkv", "subtitles": {"ja": [{"ext": "vtt", "name": "Japanese"}]}})
    _sql(settings, f"UPDATE video SET info = '{info}' WHERE id = 'm1'")
    (directory / "m1.ja.vtt").write_text(VTT_JA, encoding="utf-8")
    (directory / "m1.zh-Hans.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (directory / "m1.live_chat.json").write_text("{}", encoding="utf-8")
    return directory


def test_subtitles_lists_stored_vtt_languages_with_labels(settings):
    directory = _subtitle_rows(settings)
    (directory / "other.en.vtt").write_text("WEBVTT\n", encoding="utf-8")
    with _api(settings) as client:
        r = client.get("/api/videos/m1/subtitles")
    assert r.status_code == 200
    assert r.json() == [
        {"lang": "ja", "label": "Japanese"},
        {"lang": "zh-Hans", "label": "zh-Hans"},
    ]


def test_subtitles_serves_the_vtt_file(settings):
    _subtitle_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/subtitles/ja")
    assert r.status_code == 200
    assert r.content.decode("utf-8") == VTT_JA
    assert r.headers["content-type"] == "text/vtt; charset=utf-8"


@pytest.mark.parametrize(
    "lang", ["en", "live_chat", "JA", "..%2F..%2Fm1.ja", "ja.vtt", "%2E%2E", "ja%00"]
)
def test_subtitles_is_404_for_an_unlisted_language(settings, lang):
    _subtitle_rows(settings)
    with _api(settings) as client:
        assert client.get(f"/api/videos/m1/subtitles/{lang}").status_code == 404


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_subtitles_is_404_until_the_video_is_complete(settings, state):
    _subtitle_rows(settings, state=state)
    with _api(settings) as client:
        assert client.get("/api/videos/m1/subtitles").status_code == 404
        assert client.get("/api/videos/m1/subtitles/ja").status_code == 404


def test_subtitles_never_leave_the_archive_root(settings):
    outside = settings.archive_root.parent / "outside"
    outside.mkdir(parents=True)
    (outside / "m1.en.vtt").write_text("WEBVTT\n", encoding="utf-8")
    _subtitle_rows(settings, dir_path="../outside")
    with _api(settings) as client:
        assert client.get("/api/videos/m1/subtitles").status_code == 404
        assert client.get("/api/videos/m1/subtitles/en").status_code == 404


def test_subtitles_need_a_login(settings):
    _subtitle_rows(settings)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/videos/m1/subtitles").status_code == 401
        assert client.get("/api/videos/m1/subtitles/ja").status_code == 401


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


def test_api_filters_videos_by_the_uploader_name_across_channels(settings):
    # Member channels of one group share its name under their own ids.
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UCa', 'KMNZ', 'KMNZ [UCa]'),"
        " ('UCb', 'KMNZ', 'KMNZ [UCb]'), ('UCc', 'Other', 'Other [UCc]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('a1', 'UCa', 'videos', 't', 'complete'), ('b1', 'UCb', 'lives', 't', 'complete'),"
        " ('c1', 'UCc', 'videos', 't', 'complete')",
    )
    with _api(settings) as client:
        page = client.get("/api/videos", params={"channel_name": "KMNZ"}).json()
    assert sorted(v["id"] for v in page["items"]) == ["a1", "b1"]
    assert page["total"] == 2
