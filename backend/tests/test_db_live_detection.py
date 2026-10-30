import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    LIVE_NOW,
    MEMBERS_ONLY_ERROR,
    NASA,
    UPCOMING,
    _cancel,
    _feed,
    _no_probe,
    _no_streams,
    _not_live,
    _record_jobs,
    _sql,
    _watch,
)
from fastapi.testclient import TestClient

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Job,
    JobKind,
    JobState,
    Video,
)
from savetube.layout import VideoType
from savetube.web.app import create_app
from savetube.web.auth import User, current_user

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


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
    )
    assert created == []
    assert await _record_jobs(sessionmaker) == []
    await engine.dispose()


@pytest.mark.anyio
async def test_premieres_get_no_record_job(settings):
    """yt-dlp gives a premiere a stream's `live_status`, with `media_type` `video`."""
    from savetube.live import check_channel

    _watch(settings)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    created = await check_channel(
        sessionmaker,
        NASA,
        fetch_feed=lambda c: _feed("QHJTl3cC1r8"),
        probe=lambda v: {**UPCOMING, "id": v, "media_type": "video"},
        probe_live=lambda c: {**LIVE_NOW, "id": "playing", "media_type": "video"},
        list_streams=_no_streams,
    )
    assert created == []
    assert await _record_jobs(sessionmaker) == []
    await engine.dispose()


@pytest.mark.anyio
async def test_an_upcoming_stream_probed_before_media_type_was_kept_gets_its_record_job(
    settings,
):
    _watch(settings)
    stored = {k: v for k, v in UPCOMING.items() if k != "media_type"}
    _sql(
        settings,
        "INSERT INTO live_seen (video_id, info, probed_at, listed_at)"
        f" VALUES ('{UPCOMING['id']}', '{json.dumps(stored)}', now(), now())",
    )
    created = await _check_once(settings, [UPCOMING["id"]], _counting([], {}))
    engine = make_engine(settings.sqlalchemy_url)
    jobs = await _record_jobs(make_sessionmaker(engine))
    await engine.dispose()
    assert [(j.id, j.video_id) for j in jobs] == [(created[0], UPCOMING["id"])]


@pytest.mark.anyio
async def test_a_probed_id_is_not_probed_again(settings):
    from savetube.live import check_channel

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    calls = []

    def probe(video_id):
        calls.append(video_id)
        return {**UPCOMING, "id": video_id, "live_status": "not_live"}

    for _ in range(2):
        await check_channel(
            sessionmaker,
            NASA,
            fetch_feed=lambda c: _feed("a", "b"),
            probe=probe,
            probe_live=_not_live,
            list_streams=_no_streams,
        )
    assert sorted(calls) == ["a", "b"]
    await engine.dispose()


async def _check_once(settings, feed, probe, probe_live=_not_live):
    """One check through its own engine, as a freshly started scheduler runs it."""
    from savetube.live import check_channel

    engine = make_engine(settings.sqlalchemy_url)
    try:
        return await check_channel(
            make_sessionmaker(engine),
            NASA,
            fetch_feed=lambda c: _feed(*feed),
            probe=probe,
            probe_live=probe_live,
            list_streams=_no_streams,
        )
    finally:
        await engine.dispose()


def _counting(calls, probes):
    def probe(video_id):
        calls.append(video_id)
        result = probes[video_id]
        if isinstance(result, Exception):
            raise result
        return result

    return probe


@pytest.mark.anyio
async def test_a_restarted_scheduler_probes_only_ids_no_earlier_check_probed(settings):
    calls = []
    probes = {v: {**UPCOMING, "id": v, "live_status": "not_live"} for v in ("a", "b", "c")}
    await _check_once(settings, ["a", "b"], _counting(calls, probes))
    await _check_once(settings, ["c", "a", "b"], _counting(calls, probes))
    assert calls == ["a", "b", "c"]


@pytest.mark.anyio
async def test_an_upcoming_stream_probed_before_a_restart_still_gets_its_record_job(settings):
    """A record job made after the restart reads the stored probe."""
    calls = []
    _watch(settings)
    _sql(settings, f"UPDATE channel SET exclude_words = '{{Artemis}}' WHERE id = '{NASA}'")
    await _check_once(settings, [UPCOMING["id"]], _counting(calls, {UPCOMING["id"]: UPCOMING}))
    _sql(settings, f"UPDATE channel SET exclude_words = '{{}}' WHERE id = '{NASA}'")
    created = await _check_once(settings, [UPCOMING["id"]], _counting(calls, {}))
    assert calls == [UPCOMING["id"]]
    engine = make_engine(settings.sqlalchemy_url)
    jobs = await _record_jobs(make_sessionmaker(engine))
    await engine.dispose()
    assert [(j.id, j.scheduled_at) for j in jobs] == [
        (created[0], datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
    ]


@pytest.mark.anyio
async def test_an_unreadable_video_is_probed_again_only_24_hours_after_its_last_probe(settings):
    from yt_dlp.utils import DownloadError

    calls = []
    probes = {"private": DownloadError("ERROR: [youtube] private: Private video")}
    for _ in range(2):
        await _check_once(settings, ["private"], _counting(calls, probes))
    assert calls == ["private"]
    _sql(settings, "UPDATE live_seen SET probed_at = now() - interval '25 hours'")
    # The second refusal starts a new 24 hours.
    for _ in range(2):
        await _check_once(settings, ["private"], _counting(calls, probes))
    assert calls == ["private", "private"]


@pytest.mark.anyio
async def test_a_readable_video_is_never_probed_again(settings):
    calls = []
    probes = {"a": {**UPCOMING, "id": "a", "live_status": "not_live"}}
    await _check_once(settings, ["a"], _counting(calls, probes))
    _sql(settings, "UPDATE live_seen SET probed_at = now() - interval '25 hours'")
    await _check_once(settings, ["a"], _counting(calls, probes))
    assert calls == ["a"]


@pytest.mark.anyio
async def test_a_check_cut_off_by_a_failing_probe_keeps_the_probes_before_it(settings):
    import urllib.error

    calls = []
    probes = {
        "a": {**UPCOMING, "id": "a", "live_status": "not_live"},
        "b": urllib.error.URLError("timed out"),
    }
    with pytest.raises(urllib.error.URLError):
        await _check_once(settings, ["a", "b"], _counting(calls, probes))
    probes["b"] = {**UPCOMING, "id": "b", "live_status": "not_live"}
    await _check_once(settings, ["a", "b"], _counting(calls, probes))
    assert calls == ["a", "b", "b"]


@pytest.mark.anyio
async def test_cleanup_forgets_a_probed_id_no_check_listed_for_30_days(settings):
    from savetube.cleanup import clean_database

    _sql(
        settings,
        "INSERT INTO live_seen (video_id, info, probed_at, listed_at)"
        " SELECT v, jsonb_build_object('id', v, 'live_status', 'not_live'), at, at FROM (VALUES"
        " ('gone', now() - interval '31 days'),"
        " ('recent', now() - interval '29 days'),"
        " ('listed', now() - interval '31 days')) AS r(v, at)",
    )
    calls = []
    probes = {
        v: {**UPCOMING, "id": v, "live_status": "not_live"} for v in ("gone", "recent", "listed")
    }
    # A check that lists the id keeps it, however old its first probe.
    await _check_once(settings, ["listed"], _counting(calls, probes))
    engine = make_engine(settings.sqlalchemy_url)
    await clean_database(make_sessionmaker(engine))
    await engine.dispose()
    await _check_once(settings, ["gone", "recent", "listed"], _counting(calls, probes))
    assert calls == ["gone"]


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
    probed = []
    retried = []

    def probe(video_id):
        probed.append(video_id)
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
            member_probe=member_probe,
            profiles=profiles,
        )

    # The member profile is not present: no second probe, as before.
    await check(["alt"])
    assert retried == []
    assert [j.video_id for j in await _record_jobs(sessionmaker)] == [LIVE_NOW["id"]]

    # A refusal waits 24 hours for its next probe, member profile or not.
    await check(["alt", "main"])
    assert retried == []
    _sql(settings, "UPDATE live_seen SET probed_at = now() - interval '25 hours'")

    # Only the members-only refusal is probed again, with the member profile.
    # The readable live is not probed again.
    await check(["alt", "main"])
    assert retried == [("memlive", "main")]
    assert probed == ["memlive", "private", LIVE_NOW["id"], "memlive", "private"]
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
    )
    jobs = await _record_jobs(sessionmaker)
    assert [(j.video_id, j.scheduled_at) for j in jobs] == [
        ("pAKqJxuxDgk", datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
    ]
    await engine.dispose()
