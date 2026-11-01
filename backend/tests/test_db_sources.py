from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    PL,
    PLAYLISTS_LISTING,
    UTAWAKU,
    VESP,
    _api,
    _archive_jobs,
    _cancel,
    _children,
    _fields,
    _no_probe,
    _parent,
    _poll,
    _sql,
)
from fastapi.testclient import TestClient

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Channel,
    Job,
    JobState,
    Source,
    Video,
    VideoSource,
    VideoState,
)
from savetube.web.app import create_app
from savetube.web.auth import User, current_user

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


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
        assert client.post(f"/api/sources/{source_id}/poll").status_code == 409

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
        assert on["archiving"] is True

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
        "deleted": 0,
        "cancelled": 0,
        "skip_words": 1,
        "skip_members": 1,
        "skip_other": 0,
        "skip_date": 1,
        "skip_live": 1,
        "skip_private": 0,
    }
