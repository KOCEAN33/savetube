import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    GUERRILLA,
    LIVE_NOW,
    NASA,
    PLAYLISTS_LISTING,
    UPCOMING,
    UTAWAKU,
    VESP,
    _api,
    _archive_jobs,
    _cancel,
    _feed,
    _no_probe,
    _no_streams,
    _record_jobs,
    _sql,
    _watch,
)

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Channel,
    Job,
    Source,
    SourceKind,
    Video,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


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
        )
    # The watching /streams tab decides alone, so the /videos tab's 3DLIVE
    # word does not record "3d".
    assert sorted(j.video_id for j in await _record_jobs(sessionmaker)) == ["utawaku"]
    # A skipped detection is not probed again.
    assert sorted(probed) == ["guerrilla", "members", "utawaku"]
    await engine.dispose()


@pytest.mark.anyio
async def test_members_all_records_a_members_only_live_whatever_its_title(settings):
    from savetube.live import check_channel

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name, watch_lives, members_all) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]', true, true)",
        "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes,"
        " include_words) VALUES"
        f" ('https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360,"
        "  '{歌枠}')",
    )

    def stream(video_id, availability):
        return {
            "id": video_id,
            "title": GUERRILLA,
            "channel_id": VESP,
            "channel": "VESPERBELL",
            "live_status": "is_upcoming",
            "release_timestamp": 1792778400,
            "availability": availability,
        }

    probes = {"members": stream("members", "subscriber_only"), "public": stream("public", "public")}
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    await check_channel(
        sessionmaker,
        VESP,
        fetch_feed=lambda c: _feed("members", "public"),
        probe=lambda v: probes[v],
        probe_live=lambda c: stream("public", "public"),
        list_streams=_no_streams,
    )
    # The public stream of the same channel still meets the words.
    assert [j.video_id for j in await _record_jobs(sessionmaker)] == ["members"]
    await engine.dispose()


@pytest.mark.anyio
async def test_keep_all_records_a_live_whatever_its_title(settings):
    from savetube.live import check_channel

    _sql(
        settings,
        "UPDATE global_words SET include_words = '{歌枠}', exclude_words = '{告知}'",
        "INSERT INTO channel (id, name, dir_name, watch_lives, keep_all) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]', true, true)",
        "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        f" ('https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360)",
    )

    def stream(video_id):
        # No include word, and an exclude word.
        return {
            "id": video_id,
            "title": "【雑談】 告知",
            "channel_id": VESP,
            "channel": "VESPERBELL",
            "live_status": "is_upcoming",
            "release_timestamp": 1792778400,
        }

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    async def detect(video_id):
        await check_channel(
            sessionmaker,
            VESP,
            fetch_feed=lambda c: _feed(video_id),
            probe=stream,
            probe_live=lambda c: stream(video_id),
            list_streams=_no_streams,
        )

    await detect("kept")
    assert [j.video_id for j in await _record_jobs(sessionmaker)] == ["kept"]
    _sql(settings, "UPDATE channel SET keep_all = false")
    await detect("dropped")
    assert [j.video_id for j in await _record_jobs(sessionmaker)] == ["kept"]
    await engine.dispose()


@pytest.mark.anyio
async def test_playlists_pass_keeps_every_playlist_entry_the_exclude_words_allow(settings):
    from savetube.poll import sync

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name, include_words, exclude_words) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]', '{{歌枠}}', '{{告知}}')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360),"
        f" (2, 'https://www.youtube.com/channel/{VESP}/playlists', 'playlists', '{VESP}',"
        "  true, 360)",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " parent_id) VALUES"
        f" (3, 'https://www.youtube.com/playlist?list=PLx', 'playlist', '{VESP}', true, 360, 2)",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('chat', '{VESP}', 'lives', '雑談', 'pending')",
        "INSERT INTO video_source (video_id, source_id) VALUES ('chat', 3)",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (101, 'archive', 'queued', 'chat', 0)",
        "SELECT setval('job_id_seq', 200)",
    )
    entries = [
        {"id": "utawaku", "title": UTAWAKU, "live_status": "was_live"},
        {"id": "chat", "title": "雑談", "live_status": "was_live"},
        {"id": "notice", "title": "雑談 告知", "live_status": "was_live"},
    ]
    playlist = {"entries": [{**e, "channel_id": VESP} for e in entries]}
    tab = {"channel_id": VESP, "channel": "VESPERBELL", "entries": entries}
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    async def run(playlists_pass, source_id, listing):
        async with sessionmaker() as session:
            await session.execute(
                sa.update(Channel).where(Channel.id == VESP).values(playlists_pass=playlists_pass)
            )
            synced = await sync(session, await session.get(Source, source_id), listing, [])
            queued = sorted(
                await session.scalars(sa.select(Job.video_id).where(Job.id.in_(synced.job_ids)))
            )
            await session.rollback()
        return dict(synced.skips), queued, synced.drops

    # Off: a playlist entry meets the words like a tab entry.
    assert await run(False, 3, playlist) == (
        {"words": 2},
        ["utawaku"],
        [(101, "chat", "words")],
    )
    # On: the include words drop for the playlist. An exclude word still skips.
    assert await run(True, 3, playlist) == ({"words": 1}, ["utawaku"], [])
    # A tab of the same channel still meets the include words. The playlist
    # still passes the queued job, so it stays.
    assert await run(True, 1, tab) == ({"words": 2}, ["utawaku"], [])
    assert await run(False, 1, tab) == ({"words": 2}, ["utawaku"], [(101, "chat", "words")])

    async with sessionmaker() as session:
        await sync(session, await session.get(Source, 3), playlist, [])
        await session.commit()
    with _api(settings) as client:

        def kept():
            preview = client.post(
                "/api/sources/3/title-preview", json={"include_words": [], "exclude_words": []}
            ).json()
            return {t["video_id"] for t in preview["titles"] if t["kept"]}

        assert kept() == {"utawaku"}
        r = client.patch(f"/api/channels/{VESP}", json={"playlists_pass": True})
        assert r.status_code == 200 and r.json()["playlists_pass"] is True
        assert kept() == {"utawaku", "chat"}
    await engine.dispose()


@pytest.mark.anyio
async def test_keep_all_skips_every_title_word_but_not_the_other_checks(settings):
    from savetube.poll import sync

    _sql(
        settings,
        "UPDATE global_words SET include_words = '{歌枠}', exclude_words = '{告知}'",
        "INSERT INTO channel (id, name, dir_name, include_words, date_after) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]', '{{3DLIVE}}', '2026-01-01')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " include_words, exclude_words, other_channels) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360,"
        "  '{歌枠}', '{雑談}', false),"
        f" (2, 'https://www.youtube.com/playlist?list=PLx', 'playlist', '{VESP}', true, 360,"
        "  '{歌枠}', '{雑談}', true)",
        "SELECT setval('job_id_seq', 200)",
    )
    entries = [
        {"id": "utawaku", "title": UTAWAKU, "live_status": "was_live"},
        {"id": "chat", "title": "雑談", "live_status": "was_live"},
        {"id": "notice", "title": "雑談 告知", "live_status": "was_live"},
        {"id": "old", "title": "雑談", "live_status": "was_live", "timestamp": 1700000000},
        {"id": "members", "title": "雑談", "availability": "subscriber_only"},
        {"id": "soon", "title": "雑談", "live_status": "is_upcoming"},
    ]
    tab = {"channel_id": VESP, "channel": "VESPERBELL", "entries": entries}
    # Another channel's video in the playlist meets the playlist channel's switch.
    playlist = {
        "entries": [{"id": "nasa", "title": "Artemis 告知", "channel_id": NASA, "channel": "NASA"}]
        + [{**e, "channel_id": VESP} for e in entries[:2]]
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    async def run(keep_all, source_id, listing):
        async with sessionmaker() as session:
            await session.execute(
                sa.update(Channel).where(Channel.id == VESP).values(keep_all=keep_all)
            )
            synced = await sync(session, await session.get(Source, source_id), listing, [])
            queued = sorted(
                await session.scalars(sa.select(Job.video_id).where(Job.id.in_(synced.job_ids)))
            )
            await session.rollback()
        return dict(synced.skips), queued

    assert await run(False, 1, tab) == ({"words": 4, "live": 1}, ["utawaku"])
    assert await run(True, 1, tab) == (
        {"live": 1, "members": 1, "date": 1},
        ["chat", "notice", "utawaku"],
    )
    assert await run(True, 2, playlist) == ({}, ["chat", "nasa", "utawaku"])

    async with sessionmaker() as session:
        await sync(session, await session.get(Source, 1), tab, [])
        await session.commit()
    with _api(settings) as client:

        def kept():
            preview = client.post(
                "/api/sources/1/title-preview", json={"include_words": [], "exclude_words": []}
            ).json()
            return {t["video_id"] for t in preview["titles"] if t["kept"]}

        assert kept() == {"utawaku"}
        r = client.patch(f"/api/channels/{VESP}", json={"keep_all": True})
        assert r.status_code == 200 and r.json()["keep_all"] is True
        assert kept() == {e["id"] for e in entries}
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
