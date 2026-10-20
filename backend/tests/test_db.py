"""Runs against a real Postgres. SAVETUBE_TEST_DATABASE_URL is an admin URL whose
role has CREATEDB. Each run creates and drops its own database, savetube_test."""

import os
from datetime import UTC, datetime, timedelta
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
TEST_DB = "savetube_test"

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


@pytest.fixture(scope="module")
def test_dsn():
    admin = Settings(database_url=ADMIN_URL)
    engine = sa.create_engine(admin.sqlalchemy_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{videos}', 'b', false, false)",
        "UPDATE rule SET date_after = '2026-01-01'",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@chan/videos', 'tab', 1, true, 360),"
        " (2, 'https://www.youtube.com/playlist?list=PL1', 'playlist', 1, true, 360),"
        " (3, 'https://www.youtube.com/@off/videos', 'tab', 1, false, 360)",
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

    deferred = []

    async def enqueue(job_id):
        deferred.append(job_id)
        return 1000 + job_id

    await poll_source(
        sessionmaker,
        1,
        settings,
        lister=lambda url, c: TAB_LISTING,
        enqueue=enqueue,
        cancel=_cancel,
    )

    async with sessionmaker() as session:
        jobs = {
            j.video_id: j for j in await session.scalars(sa.select(Job).where(Job.id.in_(deferred)))
        }
        # `past_live` is a live, which the rule's types leave out.
        assert sorted(jobs) == ["new", "retry", "undated"]
        assert all(j.queue_job_id == 1000 + j.id for j in jobs.values())
        assert (await session.get(Video, "new")).state is VideoState.PENDING
        assert (await session.get(Video, "new")).channel_id == "UC1"
        assert await session.get(Video, "upcoming") is None
        assert (await session.get(Channel, "UC1")).dir_name == "Chan [UC1]"
        source = await session.get(Source, 1)
        assert source.channel_id == "UC1"
        assert source.last_polled_at is not None and source.last_error is None
        linked = set(
            await session.scalars(sa.select(VideoSource.video_id).where(VideoSource.source_id == 1))
        )
        assert linked == {"new", "undated", "done", "gave_up", "retry", "queued"}
        assert await due_sources(session, datetime.now(UTC)) == [2]

    # A second poll of the same listing finds every video queued or done.
    deferred.clear()
    async with sessionmaker() as session:
        await session.execute(sa.update(Source).values(last_polled_at=None))
        await session.commit()
    await poll_source(
        sessionmaker,
        1,
        settings,
        lister=lambda url, c: TAB_LISTING,
        enqueue=enqueue,
        cancel=_cancel,
    )
    assert deferred == []

    async def broken_enqueue(job_id):
        raise RuntimeError("queue down")

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
        enqueue=broken_enqueue,
        cancel=_cancel,
    )
    async with sessionmaker() as session:
        assert (await session.get(Channel, "UC2")).dir_name == "Two [UC2]"
        # A playlist source takes its channel from the listing.
        assert (await session.get(Source, 2)).channel_id == "UC1"
        assert await session.get(Video, "private") is None
        job = await session.scalar(sa.select(Job).where(Job.video_id == "other"))
        assert job.state is JobState.FAILED and "queue down" in job.log_tail

    def fails(url, cookiefile):
        raise RuntimeError("HTTP Error 429")

    await poll_source(sessionmaker, 2, settings, lister=fails, enqueue=enqueue, cancel=_cancel)
    async with sessionmaker() as session:
        assert (await session.get(Source, 2)).last_error == "RuntimeError: HTTP Error 429"
    await engine.dispose()


def test_api_writes_sources_and_rules(settings):
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    rule = {"name": "all", "types": ["videos", "shorts"], "date_after": "2026-01-01"}
    with TestClient(app, headers={"X-Requested-With": "savetube"}) as client:
        assert client.post("/api/rules", json=rule).status_code == 201
        assert client.post("/api/rules", json=rule).status_code == 409
        rule_id = client.get("/api/rules").json()[0]["id"]
        replaced = client.put(f"/api/rules/{rule_id}", json=rule | {"use_cookies": True})
        assert replaced.json()["use_cookies"] is True
        assert replaced.json()["date_after"] == "2026-01-01"

        body = {
            "url": "https://www.youtube.com/@chan",
            "tabs": ["videos", "shorts"],
            "rule_id": rule_id,
        }
        created = client.post("/api/sources", json=body)
        assert created.status_code == 201
        assert [s["url"] for s in created.json()] == [
            "https://www.youtube.com/@chan/videos",
            "https://www.youtube.com/@chan/shorts",
        ]
        assert client.post("/api/sources", json=body | {"tabs": ["shorts"]}).status_code == 409
        assert client.post("/api/sources", json=body | {"tabs": []}).status_code == 422
        assert client.post("/api/sources", json=body | {"rule_id": 999}).status_code == 422

        source_id = created.json()[0]["id"]
        patched = client.patch(f"/api/sources/{source_id}", json={"enabled": False})
        assert patched.json()["enabled"] is False
        assert patched.json()["poll_interval_minutes"] == 360
        assert client.post(f"/api/sources/{source_id}/poll").status_code == 202

        assert client.delete(f"/api/rules/{rule_id}").status_code == 409
        for s in created.json():
            assert client.delete(f"/api/sources/{s['id']}").status_code == 204
        assert client.delete(f"/api/rules/{rule_id}").status_code == 204
        assert client.get("/api/sources").json() == []


def test_api_refuses_a_write_without_the_csrf_header(settings):
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    with TestClient(app) as client:
        response = client.post("/api/rules", json={"name": "x", "types": ["videos"]})
        assert response.status_code == 403
        assert client.get("/api/rules").json() == []


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
    """A record job needs an enabled tab source of the channel whose rule watches lives."""
    _sql(
        settings,
        "INSERT INTO rule (name, types, format, use_cookies, watch_lives)"
        " VALUES ('watch', '{lives}', 'b', false, true)",
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{channel_id}', 'NASA', 'NASA [{channel_id}]')",
        "INSERT INTO source (url, kind, channel_id, rule_id, enabled, poll_interval_minutes)"
        f" SELECT 'https://www.youtube.com/channel/{channel_id}/streams', 'tab', '{channel_id}',"
        "  id, true, 360 FROM rule WHERE name = 'watch'",
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
async def test_watched_channels_are_enabled_tabs_whose_rule_watches_lives(settings):
    from savetube.live import watched_channels

    _sql(
        settings,
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'watch', '{lives}', 'b', false, true),"
        "        (2, 'quiet', '{lives}', 'b', false, false)",
        "INSERT INTO channel (id, name, dir_name) VALUES"
        " ('UC1', 'a', 'a'), ('UC2', 'b', 'b'), ('UC3', 'c', 'c')",
        "INSERT INTO source (url, kind, channel_id, rule_id, enabled, poll_interval_minutes)"
        " VALUES ('https://www.youtube.com/@a/streams', 'tab', 'UC1', 1, true, 360),"
        "        ('https://www.youtube.com/@a/videos', 'tab', 'UC1', 1, true, 360),"
        "        ('https://www.youtube.com/@b/streams', 'tab', 'UC2', 2, true, 360),"
        "        ('https://www.youtube.com/@c/streams', 'tab', 'UC3', 1, false, 360),"
        "        ('https://www.youtube.com/playlist?list=PL1', 'playlist', NULL, 1, true, 360)",
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, true)",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/@VESPERBELL/streams', 'tab', 1, true, 360)",
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

    async def enqueue(job_id):
        return job_id

    await poll_source(
        sessionmaker, 1, settings, lister=lambda u, c: listing, enqueue=enqueue, cancel=_cancel
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
    from savetube.web.api import get_lister

    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    if lister is not None:
        app.dependency_overrides[get_lister] = lambda: lister
    return TestClient(app, headers={"X-Requested-With": "savetube"})


async def _enqueue(job_id):
    return job_id


async def _cancel(queue_job_id):
    return True


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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, false)",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes,"
        " include_words, exclude_words) VALUES"
        " (1, 'https://www.youtube.com/@VESPERBELL/streams', 'tab', 1, true, 360,"
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
        sessionmaker, 1, settings, lister=lambda u, c: listing, enqueue=_enqueue, cancel=_cancel
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
        sessionmaker, 1, settings, lister=lambda u, c: listing, enqueue=_enqueue, cancel=_cancel
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, false),"
        "        (2, 'other', '{videos}', 'b', false, false)",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes,"
        " include_words, exclude_words, other_channels) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/playlists', 'playlists', 1, true, 120,"
        "  '{歌枠}', '{告知}', true),"
        " (2, 'https://www.youtube.com/playlist?list=PLu4d2yplfpFdB', 'playlist', 2, true, 360,"
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
            enqueue=_enqueue,
            cancel=_cancel,
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
            c.rule_id,
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
            1,
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, false)",
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, rule_id, enabled, poll_interval_minutes)"
        f" VALUES (1, 'https://www.youtube.com/channel/{VESP}/playlists', 'playlists', '{VESP}',"
        "  1, false, 360)",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes, parent_id,"
        " other_channels) VALUES"
        " (2, 'https://www.youtube.com/playlist?list=PLown', 'playlist', 1, true, 360, 1, false),"
        " (3, 'https://www.youtube.com/playlist?list=PLall', 'playlist', 1, true, 360, 1, true)",
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
        sessionmaker, 2, settings, lister=lambda u, c: listing, enqueue=_enqueue, cancel=_cancel
    )
    assert await _archive_jobs(sessionmaker) == ["own"]
    async with sessionmaker() as session:
        assert (await session.get(Source, 2)).channel_id == VESP

    await poll_source(
        sessionmaker, 3, settings, lister=lambda u, c: listing, enqueue=_enqueue, cancel=_cancel
    )
    assert await _archive_jobs(sessionmaker) == ["EoPQm9SemWw", "own"]
    await engine.dispose()


def test_api_sources_carry_title_words_and_list_channel_playlists(settings):
    listed = []

    def lister(url, cookiefile):
        listed.append(url)
        return PLAYLISTS_LISTING

    with _api(settings, lister) as client:
        rule_id = client.post("/api/rules", json={"name": "r", "types": ["lives"]}).json()["id"]
        created = client.post(
            "/api/sources",
            json={
                "url": f"https://www.youtube.com/channel/{VESP}",
                "tabs": ["streams", "playlists"],
                "rule_id": rule_id,
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
            json={
                "url": "https://www.youtube.com/playlist?list=PLu4d2yplfpFdB",
                "rule_id": rule_id,
            },
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'watch', '{lives}', 'b', false, true),"
        "        (2, 'quiet', '{lives}', 'b', false, false)",
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (url, kind, channel_id, rule_id, enabled, poll_interval_minutes,"
        " include_words, exclude_words) VALUES"
        f" ('https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', 1, true, 360,"
        "  '{歌枠}', '{メン限}'),"
        f" ('https://www.youtube.com/channel/{VESP}/videos', 'tab', '{VESP}', 1, true, 360,"
        "  '{3DLIVE}', '{}'),"
        # Empty words pass every title, but these two never watch: one is
        # disabled, the other's rule does not watch lives.
        f" ('https://www.youtube.com/channel/{VESP}/shorts', 'tab', '{VESP}', 1, false, 360,"
        "  '{}', '{}'),"
        f" ('https://www.youtube.com/playlist?list=PLx', 'playlist', '{VESP}', 2, true, 360,"
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{videos,lives}', 'b', false, false)",
        "UPDATE global_words SET include_words = '{歌枠}', exclude_words = '{告知}'",
        "INSERT INTO channel (id, name, dir_name, include_words) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]', '{{cover}}')",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes,"
        " exclude_words) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', 1, true, 360,"
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
        sessionmaker, 1, settings, lister=lambda u, c: listing, enqueue=_enqueue, cancel=_cancel
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

    def lister(url, cookiefile):
        raise DownloadError("ERROR: [youtube:tab] @gone: This channel does not exist.")

    with _api(settings, lister) as client:
        response = client.post(
            "/api/channel-playlists", json={"url": "https://www.youtube.com/@gone"}
        )
    assert response.status_code == 502
    assert "This channel does not exist." in response.json()["detail"]


KMNZ = "UCwuS0uY-Z2Gr_5OV2oFybFA"
PL = "https://www.youtube.com/playlist?list=PLu4d2yplfpFd"


def _rule_and_parent(settings, *extra, enabled="true", other_channels="false"):
    _sql(
        settings,
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, false),"
        "        (2, 'other', '{videos}', 'b', false, false)",
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, rule_id, enabled, poll_interval_minutes,"
        " include_words, other_channels) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/playlists', 'playlists', '{VESP}', 1,"
        f"  {enabled}, 120, '{{歌枠}}', {other_channels})",
        "SELECT setval('source_id_seq', 1)",
        *extra,
    )


async def _poll(sessionmaker, settings, source_id, listing, cancel=_cancel):
    from savetube.poll import poll_source

    await poll_source(
        sessionmaker,
        source_id,
        settings,
        lister=lambda u, c: listing,
        enqueue=_enqueue,
        cancel=cancel,
    )


async def _children(sessionmaker):
    async with sessionmaker() as session:
        return list(
            await session.scalars(
                sa.select(Source).where(Source.parent_id == 1).order_by(Source.url)
            )
        )


@pytest.mark.anyio
async def test_a_deleted_child_stays_deleted_until_restored(settings):
    _rule_and_parent(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    a = (await _children(sessionmaker))[0]
    assert a.url == f"{PL}A"

    with _api(settings) as client:
        assert client.delete(f"/api/sources/{a.id}").status_code == 204
        parent = next(s for s in client.get("/api/sources").json() if s["id"] == 1)
        assert parent["ignored_urls"] == [f"{PL}A"]
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    assert [c.url for c in await _children(sessionmaker)] == [f"{PL}B", f"{PL}C"]

    with _api(settings) as client:
        restored = client.patch("/api/sources/1", json={"ignored_urls": []})
        assert restored.json()["ignored_urls"] == []
    await _poll(sessionmaker, settings, 1, PLAYLISTS_LISTING)
    assert [c.url for c in await _children(sessionmaker)] == [f"{PL}A", f"{PL}B", f"{PL}C"]
    await engine.dispose()


@pytest.mark.anyio
async def test_new_children_spread_their_first_poll_over_the_interval(settings):
    from savetube.poll import due_sources

    _rule_and_parent(settings)
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

    _rule_and_parent(
        settings,
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes, parent_id)"
        f" VALUES (2, '{PL}A', 'playlist', 1, true, 120, 1),"
        "        (3, 'https://www.youtube.com/playlist?list=PLlone', 'playlist', 1, true, 120,"
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
    _rule_and_parent(
        settings,
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes, parent_id,"
        " include_words) VALUES (2, '{PL}A', 'playlist', 1, true, 120, 1, '{Cover}')".replace(
            "{PL}", PL
        ),
    )
    with _api(settings) as client:
        response = client.patch(
            "/api/sources/1",
            json={
                "rule_id": 2,
                "poll_interval_minutes": 60,
                "other_channels": True,
                "include_words": ["告知"],
                "enabled": False,
            },
        )
        assert response.status_code == 200
        child = next(s for s in client.get("/api/sources").json() if s["id"] == 2)
    assert (
        child["rule_id"],
        child["poll_interval_minutes"],
        child["other_channels"],
        child["include_words"],
        child["enabled"],
    ) == (2, 60, True, ["Cover"], True)


@pytest.mark.anyio
async def test_a_child_merges_parent_words_and_each_uploaders_channel_words(settings):
    _rule_and_parent(
        settings,
        f"UPDATE channel SET exclude_words = '{{告知}}' WHERE id = '{VESP}'",
        "INSERT INTO channel (id, name, dir_name, exclude_words) VALUES"
        f" ('{KMNZ}', 'KMNZ', 'KMNZ [{KMNZ}]', '{{ヨミネロ}}')",
        "INSERT INTO source (id, url, kind, channel_id, rule_id, enabled, poll_interval_minutes,"
        f" parent_id, other_channels) VALUES (2, '{PL}A', 'playlist', '{VESP}', 1, true, 120, 1,"
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, false)",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes) VALUES"
        " (1, 'https://www.youtube.com/playlist?list=PLmix', 'playlist', 1, true, 360)",
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, false)",
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, rule_id, enabled, poll_interval_minutes,"
        " exclude_words) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', 1, true, 360,"
        "  '{告知}'),"
        f" (2, 'https://www.youtube.com/channel/{VESP}/videos', 'tab', '{VESP}', 1, true, 360,"
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
async def test_a_playlist_gone_from_the_channel_disables_its_child(settings):
    _rule_and_parent(settings)
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
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, false)",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', 1, true, 360)",
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
        "INSERT INTO source (url, kind, channel_id, rule_id, enabled, poll_interval_minutes,"
        " include_words)"
        f" SELECT 'https://www.youtube.com/channel/{NASA}/videos', 'tab', '{NASA}', id, true, 360,"
        "  '{}' FROM rule WHERE name = 'watch'",
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

    def slow(url, cookiefile):
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
    return f"UPDATE download_settings SET window_start = '{start}', window_end = '{end}'"


class _Runner:
    def __init__(self):
        self.params = None

    async def __call__(self, url, params):
        from savetube.jobs import DownloadFailed

        self.params = params
        raise DownloadFailed("stopped by the test")


async def _run(settings, *, live=False, defer=None):
    from savetube.jobs import run

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    runner = _Runner()
    deferred = []

    async def record_defer(job_id, at):
        deferred.append((job_id, at))
        return 900

    try:
        await run(
            1,
            settings,
            live=live,
            runner=runner,
            defer=defer or record_defer,
            sessionmaker=sessionmaker,
        )
    except Exception as e:
        if type(e).__name__ != "DownloadFailed":
            raise
    async with sessionmaker() as session:
        job = await session.get(Job, 1)
    await engine.dispose()
    return job, runner, deferred


@pytest.mark.anyio
async def test_an_archive_outside_the_window_defers_to_its_start(settings):
    _job_rows(settings, _closed_window_sql())
    before = datetime.now(UTC)
    job, runner, deferred = await _run(settings)
    assert runner.params is None
    assert job.state is JobState.QUEUED and job.attempts == 0
    assert before + timedelta(minutes=118) < job.deferred_until <= before + timedelta(hours=2)
    assert deferred == [(1, job.deferred_until)]
    assert job.queue_job_id == 900


@pytest.mark.anyio
async def test_an_archive_without_a_free_slot_defers_a_minute(settings):
    _job_rows(
        settings,
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1)",
    )
    before = datetime.now(UTC)
    job, runner, deferred = await _run(settings)
    assert runner.params is None and job.state is JobState.QUEUED
    assert before + timedelta(seconds=59) < job.deferred_until < before + timedelta(seconds=75)
    assert [j for j, _ in deferred] == [1]


@pytest.mark.anyio
async def test_an_admitted_archive_runs_with_the_rate_limit(settings):
    _job_rows(
        settings,
        "UPDATE download_settings SET max_concurrent = 2, rate_limit_bytes = 1000000",
        "UPDATE job SET deferred_until = now() WHERE id = 1",
        # A running recording takes no archive slot.
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (2, 'archive', 'running', 'busy1', 1), (3, 'record', 'running', 'busy2', 1)",
    )
    job, runner, deferred = await _run(settings)
    assert deferred == []
    assert runner.params["ratelimit"] == 1000000
    assert job.attempts == 1 and job.started_at is not None
    assert job.deferred_until is None
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
    job, runner, deferred = await _run(settings, live=True)
    assert deferred == []
    assert runner.params is not None and "ratelimit" not in runner.params


@pytest.mark.anyio
async def test_a_failed_defer_fails_the_job(settings):
    _job_rows(settings, _closed_window_sql())

    async def broken(job_id, at):
        raise RuntimeError("queue down")

    job, runner, _ = await _run(settings, defer=broken)
    assert job.state is JobState.FAILED and "queue down" in job.log_tail


def test_api_download_settings_and_job_deferral(settings):
    _job_rows(settings, "UPDATE job SET deferred_until = '2026-10-21T14:00:00Z'")
    with _api(settings) as client:
        assert client.get("/api/download-settings").json() == {
            "max_concurrent": 1,
            "rate_limit_bytes": None,
            "window_start": None,
            "window_end": None,
            "timezone": "Asia/Seoul",
        }
        body = {
            "max_concurrent": 3,
            "rate_limit_bytes": 2000000,
            "window_start": "23:00",
            "window_end": "05:00",
        }
        put = client.put("/api/download-settings", json=body)
        assert put.status_code == 200
        assert put.json() == body | {"timezone": "Asia/Seoul"}
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
        open_all_day = body | {"window_start": None, "window_end": None}
        assert client.put("/api/download-settings", json=open_all_day).status_code == 200

        [job] = client.get("/api/jobs").json()
        assert job["deferred_until"] == "2026-10-21T14:00:00Z"
        rule = client.post("/api/rules", json={"name": "r", "types": ["videos"]}).json()
        assert "max_concurrent" not in rule


@pytest.mark.anyio
async def test_a_source_lists_without_downloading_until_archiving_is_on(settings):
    _sql(
        settings,
        "INSERT INTO rule (id, name, types, format, use_cookies, watch_lives)"
        " VALUES (1, 'r', '{lives}', 'b', false, false)",
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
            json={"url": f"https://www.youtube.com/channel/{VESP}/streams", "rule_id": 1},
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
    _rule_and_parent(
        settings,
        "UPDATE source SET archiving = false WHERE id = 1",
        "INSERT INTO source (id, url, kind, rule_id, enabled, poll_interval_minutes, parent_id,"
        f" archiving) VALUES (2, '{PL}A', 'playlist', 1, true, 120, 1, false)",
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
