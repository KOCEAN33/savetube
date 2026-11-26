"""Channel memberships of the cookie profiles. A members-only download takes a
profile whose membership in the channel a check confirmed."""

import asyncio
import logging
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yt_dlp.utils import DownloadError

from savetube import limits, logs
from savetube.config import Settings
from savetube.cookies import cookiefile, general, profiles
from savetube.db.models import Channel, CookieProfile, Membership, Video
from savetube.layout import VideoType
from savetube.logs import emit
from savetube.ytdl import members_level, members_only, youtube_block

log = logging.getLogger(__name__)

# A membership result older than this is checked again by the next poll that
# lists a members-only video of the channel.
CHECK_EVERY = timedelta(hours=24)

# Reads a members-only video id with a copy of one profile's cookie file, and
# returns its info dict with formats. Raises when the account may not watch it.
Prober = Callable[[str, Path], dict[str, Any]]
# YouTube refuses a video of a higher membership level with the same text to a
# lower-level member and to a non-member, so such a refusal decides nothing and
# a check tries the next listed video, up to this many.
PROBES_PER_CHECK = 3


async def member_channels(
    session: AsyncSession, channel_ids: list[str], present: list[str]
) -> set[str]:
    """The channels among `channel_ids` that a present profile is a member of."""
    if not channel_ids or not present:
        return set()
    return set(
        await session.scalars(
            select(Membership.channel_id).where(
                Membership.channel_id.in_(channel_ids),
                Membership.profile.in_(present),
                Membership.is_member,
            )
        )
    )


async def member_profile(session: AsyncSession, channel_id: str, present: list[str]) -> str | None:
    """The first present profile by name that is a member of the channel."""
    if not present:
        return None
    return await session.scalar(
        select(Membership.profile)
        .where(
            Membership.channel_id == channel_id,
            Membership.profile.in_(present),
            Membership.is_member,
        )
        .order_by(Membership.profile)
        .limit(1)
    )


async def for_video(session: AsyncSession, video: Video, present: list[str]) -> str | None:
    """The profile a download of `video` takes: a member profile serves its
    members-only videos only, every other download takes the general profile."""
    if video.type is VideoType.MEMBERS:
        return await member_profile(session, video.channel_id, present)
    return await general(session)


def _member_pick():
    """The member profile `for_video` picks for the outer `Video` row, with
    the present profiles read from `cookie_profile`."""
    return (
        select(Membership.profile)
        .where(
            Membership.channel_id == Video.channel_id,
            Membership.profile.in_(select(CookieProfile.name)),
            Membership.is_member,
        )
        .order_by(Membership.profile)
        .limit(1)
        .scalar_subquery()
    )


def videos_using(profile: str, general_profile: str | None) -> Select:
    """The ids of the videos whose download takes `profile`, as `for_video`
    picks it."""
    takes = (Video.type == VideoType.MEMBERS) & (_member_pick() == profile)
    if profile == general_profile:
        takes = takes | (Video.type != VideoType.MEMBERS)
    return select(Video.id).where(takes)


def videos_without_member() -> Select:
    """The ids of the members-only videos no present profile is a confirmed
    member for, such as after a replaced cookie file dropped the results."""
    return select(Video.id).where(Video.type == VideoType.MEMBERS, _member_pick().is_(None))


async def _due(session: AsyncSession, channel_id: str, present: list[str], now: datetime):
    fresh = set(
        await session.scalars(
            select(Membership.profile).where(
                Membership.channel_id == channel_id,
                Membership.checked_at > now - CHECK_EVERY,
            )
        )
    )
    return [p for p in present if p not in fresh]


async def check_memberships(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    listed: dict[str, list[str]],
    *,
    prober: Prober,
) -> None:
    """Check each present profile whose result for a channel is missing or
    stale. `listed` maps a channel id to the members-only video ids a poll
    listed for it. One check at a time, none while a YouTube block pauses
    downloads, and none after a check YouTube refused."""
    present = profiles(settings)
    if not listed or not present:
        return
    for channel_id, video_ids in listed.items():
        async with sessionmaker() as session:
            channel = await session.get(Channel, channel_id)
            if channel is None:
                continue
            due = await _due(session, channel_id, present, datetime.now(UTC))
        for profile in due:
            async with sessionmaker() as session:
                if limits.blocked(await limits.load(session), datetime.now(UTC)) is not None:
                    return
                candidates = await _unleveled(session, video_ids)
            checked = await _check(
                sessionmaker, settings, channel, profile, candidates, prober=prober
            )
            if not checked:
                return


async def _unleveled(session: AsyncSession, video_ids: list[str]) -> list[str]:
    """`video_ids` in order, without those a refusal named a membership level for."""
    leveled = set(
        await session.scalars(
            select(Video.id).where(Video.id.in_(video_ids), Video.members_level.is_not(None))
        )
    )
    return [v for v in video_ids if v not in leveled]


async def _check(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    channel: Channel,
    profile: str,
    video_ids: list[str],
    *,
    prober: Prober,
) -> bool:
    """One check, with the first of `video_ids` whose answer is not a refusal
    naming a membership level, of at most PROBES_PER_CHECK. Returns False when
    YouTube refused this client, and the caller then checks no other profile."""
    for video_id in video_ids[:PROBES_PER_CHECK]:
        fields = dict(channel=channel.name, profile=profile, video=video_id)
        try:
            with tempfile.TemporaryDirectory(prefix="savetube-") as private:
                path = cookiefile(settings, profile, Path(private))
                info = await asyncio.to_thread(prober, video_id, path)
            is_member = bool(info.get("formats"))
            if not is_member:
                raise DownloadError("the video lists no formats")
        except Exception as e:
            async with sessionmaker() as session:
                level = await note_level(session, video_id, str(e))
                await session.commit()
            if level is not None:
                emit(log, logging.INFO, "membership.level_refused", **fields, level=level)
                continue
            if not members_only(str(e)):
                expected = isinstance(e, DownloadError)
                emit(
                    log,
                    logging.WARNING,
                    "membership.check_failed",
                    **fields,
                    reason=logs.cut(f"{type(e).__name__}: {e}"),
                    exc_info=not expected,
                )
                return not youtube_block(str(e))
            is_member = False
        row = dict(
            channel_id=channel.id,
            profile=profile,
            is_member=is_member,
            checked_at=datetime.now(UTC),
            video_id=video_id,
        )
        async with sessionmaker() as session:
            stmt = insert(Membership).values(**row)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Membership.channel_id, Membership.profile],
                    set_={k: stmt.excluded[k] for k in ("is_member", "checked_at", "video_id")},
                )
            )
            await session.commit()
        emit(log, logging.INFO, "membership.checked", **fields, member="yes" if is_member else "no")
        return True
    return True


async def note_level(session: AsyncSession, video_id: str, message: str) -> str | None:
    """Store on the video the membership level a refusal `message` names, and
    return it. None when the message names none. The caller commits."""
    level = members_level(message)
    if level is not None:
        await session.execute(update(Video).where(Video.id == video_id).values(members_level=level))
    return level


async def mark_members(
    sessionmaker: async_sessionmaker, settings: Settings, video_id: str
) -> str | None:
    """Mark the video members-only. Returns its channel's member profile, None when the
    video or the profile is missing."""
    async with sessionmaker() as session:
        video = await session.get(Video, video_id)
        if video is None:
            return None
        video.type = VideoType.MEMBERS
        video.availability = "subscriber_only"
        profile = await member_profile(session, video.channel_id, profiles(settings))
        await session.commit()
    return profile
