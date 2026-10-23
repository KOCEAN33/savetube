import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import (
    ADMIN_URL,
    MEMBERS_ONLY_ERROR,
    VESP,
    _api,
    _cancel,
    _fields,
    _finished,
    _job_rows,
    _no_probe,
    _poll,
    _run,
    _run_steps,
    _Runner,
    _sql,
    _Steps,
)

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Channel,
    Job,
    JobState,
    ListingEntry,
    Source,
    Video,
    VideoSource,
)
from savetube.layout import VideoType

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


def _profiles(settings, *names):
    """Settings whose SAVETUBE_COOKIES_DIR holds a file per name, its content `# <name>`."""
    root = settings.archive_root.parent / "cookies"
    root.mkdir(exist_ok=True)
    for name in names:
        (root / f"{name}.txt").write_text(f"# {name}\n")
    # A Secret mount's own entries are no profiles.
    (root / "..data").mkdir(exist_ok=True)
    return settings.model_copy(update={"cookies_dir": root})


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
async def test_members_all_exempts_members_only_videos_from_the_words(settings):
    from savetube.poll import sync

    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " include_words) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/streams', 'tab', '{VESP}', true, 360,"
        "  '{歌枠}')",
        "INSERT INTO membership (channel_id, profile, is_member, video_id, checked_at) VALUES"
        f" ('{VESP}', 'main', true, 'member_chat', now())",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('member_chat', '{VESP}', 'members', '雑談', 'pending')",
        "INSERT INTO video_source (video_id, source_id) VALUES ('member_chat', 1)",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (101, 'archive', 'queued', 'member_chat', 0)",
        "SELECT setval('job_id_seq', 200)",
    )
    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [
            {"id": "member_chat", "title": "【メン限】雑談", "availability": "subscriber_only"},
            {"id": "public_chat", "title": "雑談", "availability": "public"},
            {"id": "utawaku", "title": "【歌枠】", "availability": "public"},
        ],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)

    async def run(members_all, profiles):
        async with sessionmaker() as session:
            await session.execute(
                sa.update(Channel).where(Channel.id == VESP).values(members_all=members_all)
            )
            synced = await sync(session, await session.get(Source, 1), listing, profiles)
            queued = sorted(
                await session.scalars(sa.select(Job.video_id).where(Job.id.in_(synced.job_ids)))
            )
            await session.rollback()
        return dict(synced.skips), queued, synced.drops

    # Without a confirmed member profile the switch changes nothing: the
    # membership check still skips it and cancels its queued job.
    assert await run(True, []) == (
        {"members": 1, "words": 1},
        ["utawaku"],
        [(101, "member_chat", "members")],
    )
    # A member keeps it although its title fails the words. A public video
    # of the same channel still meets them.
    assert await run(True, ["main"]) == ({"words": 1}, ["utawaku"], [])
    assert await run(False, ["main"]) == (
        {"words": 2},
        ["utawaku"],
        [(101, "member_chat", "words")],
    )
    await engine.dispose()


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
            {"name": "main", "account_name": None, "account_handle": None},
            {"name": "throwaway", "account_name": None, "account_handle": None},
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
async def test_each_profile_account_is_read_once_per_file_content(settings, caplog):
    import logging

    from yt_dlp.utils import DownloadError

    from savetube.cookies import read_accounts, record_profiles
    from savetube.ytdl import NotSignedIn

    settings = _profiles(settings, "alt", "main")
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        await record_profiles(session, ["alt", "main"], datetime.now(UTC))
        await session.commit()
    read = []
    accounts = {"main": ("vivi kikirara", "@vivikikirara"), "alt": NotSignedIn()}

    def reader(cookiefile):
        profile = cookiefile.read_text().splitlines()[0].removeprefix("# ")
        read.append(profile)
        account = accounts[profile]
        if isinstance(account, Exception):
            raise account
        return account

    def rows():
        engine = sa.create_engine(settings.sqlalchemy_url)
        with engine.connect() as c:
            found = c.execute(
                sa.text(
                    "SELECT name, account_name, account_handle, account_digest IS NOT NULL,"
                    " account_checked_at IS NOT NULL FROM cookie_profile ORDER BY name"
                )
            ).all()
        engine.dispose()
        return [tuple(r) for r in found]

    # No read while a YouTube block pauses downloads.
    _sql(settings, "UPDATE download_settings SET blocked_until = now() + interval '1 hour'")
    await read_accounts(sessionmaker, settings, reader=reader)
    assert read == []
    _sql(settings, "UPDATE download_settings SET blocked_until = NULL")

    with caplog.at_level(logging.INFO, logger="savetube"):
        await read_accounts(sessionmaker, settings, reader=reader)
    assert read == ["alt", "main"]
    assert rows() == [
        ("alt", None, None, False, True),
        ("main", "vivi kikirara", "@vivikikirara", True, True),
    ]
    [failed] = [r for r in caplog.records if r.getMessage() == "cookies.account_failed"]
    assert (failed.levelno, _fields(failed)) == (
        logging.WARNING,
        {"profile": "alt", "reason": "not signed in"},
    )
    assert failed.exc_info is None
    [ok] = [r for r in caplog.records if r.getMessage() == "cookies.account"]
    assert _fields(ok) == {"profile": "main", "name": "vivi kikirara", "handle": "@vivikikirara"}
    assert "# main" not in caplog.text and "# alt" not in caplog.text

    # Same content, and a failure under an hour old: no read.
    await read_accounts(sessionmaker, settings, reader=reader)
    assert read == ["alt", "main"]

    def memberships():
        engine = sa.create_engine(settings.sqlalchemy_url)
        with engine.connect() as c:
            found = c.execute(sa.text("SELECT profile FROM membership ORDER BY profile")).all()
        engine.dispose()
        return [r[0] for r in found]

    _sql(
        settings,
        f"INSERT INTO channel (id, name, dir_name) VALUES ('{VESP}', 'V', 'V [{VESP}]')",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        f" ('{VESP}', 'main', true, now(), 'm1'), ('{VESP}', 'alt', false, now(), 'm1')",
    )

    # A changed file reads again at once, and a replaced file that read
    # before drops its membership results. A failed one waits for the hour.
    (settings.cookies_dir / "main.txt").write_text("# main\n# rotated\n")
    (settings.cookies_dir / "alt.txt").write_text("# alt\n# signed in\n")
    accounts = {"main": ("other", None), "alt": ("alt account", "@alt")}
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="savetube"):
        await read_accounts(sessionmaker, settings, reader=reader)
    assert read == ["alt", "main", "main"]
    assert rows()[1] == ("main", "other", None, True, True)
    assert memberships() == ["alt"]
    [changed] = [r for r in caplog.records if r.getMessage() == "cookies.changed"]
    assert _fields(changed) == {"profile": "main", "memberships": 1}
    _sql(
        settings,
        "UPDATE cookie_profile SET account_checked_at = now() - interval '2 hours'"
        " WHERE name = 'alt'",
    )
    await read_accounts(sessionmaker, settings, reader=reader)
    assert read == ["alt", "main", "main", "alt"]
    assert rows()[0] == ("alt", "alt account", "@alt", True, True)
    # The first successful read after a failure had no digest to replace.
    assert memberships() == ["alt"]

    # A bot check stops the reads of this tick and clears the stale account.
    for name in ("alt", "main"):
        (settings.cookies_dir / f"{name}.txt").write_text(f"# {name}\n# third\n")
    accounts = {"alt": DownloadError(BOT_CHECK_ERROR), "main": ("never", None)}
    await read_accounts(sessionmaker, settings, reader=reader)
    assert read == ["alt", "main", "main", "alt", "alt"]
    assert rows()[0] == ("alt", None, None, False, True)
    assert memberships() == []

    with _api(settings) as client:
        assert client.get("/api/cookie-profiles").json() == [
            {"name": "alt", "account_name": None, "account_handle": None},
            {"name": "main", "account_name": "other", "account_handle": None},
        ]
    await engine.dispose()


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


def _cookie_step(cookies, then):
    """A runner step that records the call's cookie profile, then runs `then`."""

    async def step(params, report):
        text = await asyncio.to_thread(Path(params["cookiefile"]).read_text)
        cookies.append(text.removeprefix("# ").strip())
        return await then(params, report)

    return step


def _refuse(message):
    async def step(params, report):
        from savetube.jobs import DownloadFailed

        raise DownloadFailed(message)

    return step


def _short_job_rows(settings, *, member):
    _job_rows(
        settings,
        "UPDATE video SET type = 'shorts' WHERE id = 'v1'",
        "UPDATE download_settings SET cookie_profile = 'general'",
        "INSERT INTO membership (channel_id, profile, is_member, checked_at, video_id) VALUES"
        f" ('UC1', 'main', {str(member).lower()}, now(), 'other')",
    )
    return _profiles(settings, "general", "main")


@pytest.mark.anyio
async def test_a_members_only_refusal_retries_once_with_the_member_profile(settings):
    settings = _short_job_rows(settings, member=True)
    cookies = []
    runner = _Steps(
        _cookie_step(cookies, _refuse(MEMBERS_ONLY_ERROR)),
        _cookie_step(cookies, _finished()),
    )
    jobs, video = await _run_steps(settings, runner)
    assert cookies == ["general", "main"]
    assert jobs[1].state is JobState.SUCCEEDED
    assert (video.type, video.availability) == (VideoType.MEMBERS, "subscriber_only")
    assert video.dir_path.split("/")[1] == "members"


@pytest.mark.anyio
async def test_a_members_only_refusal_without_a_member_profile_fails_once(settings):
    settings = _short_job_rows(settings, member=False)
    cookies = []
    runner = _Steps(_cookie_step(cookies, _refuse(MEMBERS_ONLY_ERROR)))
    jobs, video = await _run_steps(settings, runner)
    assert cookies == ["general"]
    assert (jobs[1].state, jobs[1].log_tail) == (JobState.FAILED, MEMBERS_ONLY_ERROR)
    assert (video.type, video.availability) == (VideoType.MEMBERS, "subscriber_only")


@pytest.mark.anyio
async def test_another_refusal_takes_no_member_profile(settings):
    settings = _short_job_rows(settings, member=True)
    cookies = []
    runner = _Steps(_cookie_step(cookies, _refuse("ERROR: [youtube] v1: Video unavailable")))
    jobs, video = await _run_steps(settings, runner)
    assert cookies == ["general"]
    assert jobs[1].state is JobState.FAILED
    assert (video.type, video.availability) == (VideoType.SHORTS, None)


@pytest.mark.anyio
async def test_a_stored_members_short_needs_a_member_on_the_next_poll(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/shorts', 'tab', '{VESP}', true, 360)",
        "INSERT INTO video (id, channel_id, type, title, state, availability) VALUES"
        f" ('short', '{VESP}', 'members', 's', 'failed', 'subscriber_only')",
    )
    settings = _profiles(settings, "main")
    # The Shorts tab lists no availability.
    listing = {
        "channel_id": VESP,
        "channel": "VESPERBELL",
        "entries": [{"id": "short", "title": "s"}, {"id": "public", "title": "p"}],
    }
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    probed = []

    def prober(video_id, cookiefile):
        probed.append(video_id)
        return {"id": video_id, "formats": [{"format_id": "251"}]}

    async def queued():
        async with sessionmaker() as session:
            return sorted(
                await session.scalars(sa.select(Job.video_id).where(Job.state == JobState.QUEUED))
            )

    # No result yet: the stored members-only short is skipped, and the poll
    # checks the profile with it.
    await _poll(sessionmaker, settings, 1, listing, prober=prober)
    assert await queued() == ["public"]
    assert probed == ["short"]
    await _poll(sessionmaker, settings, 1, listing, prober=prober)
    assert await queued() == ["public", "short"]
    async with sessionmaker() as session:
        assert (await session.get(Video, "short")).type is VideoType.MEMBERS
    await engine.dispose()
