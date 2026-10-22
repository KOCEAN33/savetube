from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    LIVE_NOW,
    NASA,
    PL,
    PLAYLISTS_LISTING,
    UPCOMING,
    UTAWAKU,
    VESP,
    _api,
    _archive_jobs,
    _children,
    _feed,
    _no_streams,
    _not_live,
    _parent,
    _poll,
    _record_jobs,
    _sql,
    _watch,
)
from fastapi.testclient import TestClient

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Job,
    JobState,
    Source,
    Video,
    VideoSource,
)
from savetube.web.app import create_app
from savetube.web.auth import User, current_user

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


KMNZ = "UCwuS0uY-Z2Gr_5OV2oFybFA"


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
