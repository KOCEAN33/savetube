"""Discord posts for what loses content unless the user acts before opening
the dashboard (docs/plan/operations.md, Notifications and monitoring)."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx2
from sqlalchemy import Exists, cast, delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.types import Text

from savetube.attention import dismissed_job, record_failed, signed_out
from savetube.config import Settings
from savetube.db.models import Channel, CookieProfile, Job, Notification, Video
from savetube.logs import emit
from savetube.ytdl import strip_noise

log = logging.getLogger(__name__)

# An older failure is never sent, so the first run after a deploy sends no backlog.
RECENT = timedelta(hours=1)
POST_SECONDS = 10
_REASON_CHARS = 300

Post = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class Message:
    kind: str
    key: str
    text: str


async def post(url: str, text: str) -> None:
    async with httpx2.AsyncClient(timeout=POST_SECONDS) as client:
        r = await client.post(url, json={"content": text, "allowed_mentions": {"parse": []}})
        r.raise_for_status()


def _sent(kind: str, key: object) -> Exists:
    return exists().where(Notification.kind == kind, Notification.key == key)


async def _records(session: AsyncSession, base: str, now: datetime) -> list[Message]:
    rows = await session.execute(
        select(Job, Video.title, Channel.name)
        .join(Video, Video.id == Job.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .where(record_failed(), Job.finished_at > now - RECENT)
        .where(~_sent("record_failed", cast(Job.id, Text)), ~dismissed_job())
        .order_by(Job.id)
    )
    out = []
    for job, title, channel_name in rows:
        if job.miss_reason is not None:
            reason = job.miss_reason.replace("_", " ")
        else:
            reason = strip_noise(job.log_tail or "")[:_REASON_CHARS] or "no reason recorded"
        state = "missed" if job.miss_reason is not None else "failed"
        out.append(
            Message(
                "record_failed",
                str(job.id),
                f"**Recording {state}**: {title}\n{channel_name} / {reason}\n"
                f"{base}/videos/{job.video_id}",
            )
        )
    return out


async def _cookies(session: AsyncSession, base: str) -> list[Message]:
    # A profile that reads again may sign out again later, which sends again.
    await session.execute(
        delete(Notification).where(
            Notification.kind == "cookie_signed_out",
            ~Notification.key.in_(select(CookieProfile.name).where(signed_out())),
        )
    )
    names = await session.scalars(
        select(CookieProfile.name)
        .where(signed_out(), ~_sent("cookie_signed_out", CookieProfile.name))
        .order_by(CookieProfile.name)
    )
    return [
        Message(
            "cookie_signed_out",
            name,
            f"**Cookie profile signed out**: {name}\n"
            "Its account read failed, such as a logged-out cookie file. Members-only "
            f"downloads and recordings with it fail until the file is replaced.\n{base}/settings",
        )
        for name in names
    ]


async def notify(
    sessionmaker: async_sessionmaker, settings: Settings, now: datetime, post: Post = post
) -> None:
    if settings.discord_webhook_url is None:
        return
    url = settings.discord_webhook_url.get_secret_value()
    base = settings.public_url.rstrip("/")
    async with sessionmaker() as session:
        messages = [*await _records(session, base, now), *await _cookies(session, base)]
        await session.commit()
        for m in messages:
            try:
                await post(url, m.text)
            except httpx2.HTTPError as e:
                emit(log, logging.WARNING, "notify.failed", kind=m.kind, key=m.key, error=str(e))
                continue
            session.add(Notification(kind=m.kind, key=m.key))
            await session.commit()
            emit(log, logging.INFO, "notify.sent", kind=m.kind, key=m.key)
