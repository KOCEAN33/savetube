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
