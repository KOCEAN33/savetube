import asyncio

import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    GUERRILLA,
    NASA,
    PL,
    PLAYLISTS_LISTING,
    UPCOMING,
    UTAWAKU,
    VESP,
    _api,
    _cancel,
    _fields,
    _job_rows,
    _no_probe,
    _no_streams,
    _not_live,
    _queue_row,
    _sql,
    _watch,
)

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Channel,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


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

    from savetube.web.api import scan

    monkeypatch.setattr(scan, "CHANNEL_PLAYLISTS_TIMEOUT", 0.2)

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
