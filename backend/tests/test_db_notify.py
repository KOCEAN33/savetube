"""Discord posts (docs/plan/operations.md, Notifications and monitoring)."""

from datetime import UTC, datetime

import httpx2
import pytest
from conftest import ADMIN_URL, _sql
from pydantic import SecretStr

from savetube.db import make_engine, make_sessionmaker

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
URL = "https://discord.test/api/webhooks/1/x"


class _Discord:
    def __init__(self, fail=False):
        self.posts = []
        self.fail = fail

    async def __call__(self, url, text):
        if self.fail:
            raise httpx2.ConnectError("unreachable")
        self.posts.append((url, text))


async def _notify(settings, discord, now=NOW, url: str | None = URL):
    from savetube.notify import notify

    settings = settings.model_copy(
        update={
            "discord_webhook_url": None if url is None else SecretStr(url),
            "public_url": "https://savetube.test",
        }
    )
    engine = make_engine(settings.sqlalchemy_url)
    try:
        await notify(make_sessionmaker(engine), settings, now, post=discord)
    finally:
        await engine.dispose()


def _recording(settings, *, video_state="pending", finished="2026-11-09T11:30:00Z", **job):
    cols = ", ".join(job)
    vals = ", ".join(f"'{v}'" for v in job.values())
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Chan', 'Chan [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        f" ('v1', 'UC1', 'lives', 'Live one', '{video_state}')",
        f"INSERT INTO job (id, kind, video_id, attempts, finished_at{', ' if job else ''}{cols})"
        f" VALUES (7, 'record', 'v1', 1, '{finished}'{', ' if job else ''}{vals})",
    )


@pytest.mark.anyio
async def test_a_missed_recording_posts_once(settings):
    _recording(settings, state="missed", miss_reason="ended_before_detection")
    discord = _Discord()

    await _notify(settings, discord)
    await _notify(settings, discord)

    assert discord.posts == [
        (
            URL,
            "**Recording missed**: Live one\nChan / ended before detection\n"
            "https://savetube.test/videos/v1",
        )
    ]


@pytest.mark.anyio
async def test_a_recording_that_failed_over_an_hour_ago_posts_nothing(settings):
    _recording(settings, state="failed", finished="2026-11-09T10:59:00Z")
    discord = _Discord()

    await _notify(settings, discord)

    assert discord.posts == []


@pytest.mark.anyio
async def test_a_failed_recording_whose_vod_is_stored_posts_nothing(settings):
    _recording(settings, state="failed", video_state="complete")
    discord = _Discord()

    await _notify(settings, discord)

    assert discord.posts == []


@pytest.mark.anyio
async def test_a_failed_post_is_sent_again_next_tick(settings):
    _recording(settings, state="failed", log_tail="recorder exited 1")

    await _notify(settings, _Discord(fail=True))
    discord = _Discord()
    await _notify(settings, discord)

    assert [t.split("\n")[:2] for _, t in discord.posts] == [
        ["**Recording failed**: Live one", "Chan / recorder exited 1"]
    ]


@pytest.mark.anyio
async def test_no_webhook_posts_nothing(settings):
    _recording(settings, state="failed")
    discord = _Discord()

    await _notify(settings, discord, url=None)

    assert discord.posts == []


@pytest.mark.anyio
async def test_a_cookie_profile_posts_again_after_it_reads_and_signs_out_again(settings):
    _sql(
        settings,
        "INSERT INTO cookie_profile (name, seen_at, account_checked_at) VALUES"
        " ('member', now(), now())",
    )
    discord = _Discord()

    await _notify(settings, discord)
    await _notify(settings, discord)
    _sql(settings, "UPDATE cookie_profile SET account_digest = 'abc'")
    await _notify(settings, discord)
    _sql(settings, "UPDATE cookie_profile SET account_digest = NULL")
    await _notify(settings, discord)

    assert [t.split("\n")[0] for _, t in discord.posts] == [
        "**Cookie profile signed out**: member",
        "**Cookie profile signed out**: member",
    ]


@pytest.mark.anyio
async def test_a_dismissed_recording_failure_posts_nothing(settings):
    _recording(settings, state="missed", miss_reason="ended_before_detection")
    _sql(settings, "INSERT INTO attention_dismissal (kind, key) VALUES ('record_failed', '7')")
    discord = _Discord()

    await _notify(settings, discord)

    assert discord.posts == []
