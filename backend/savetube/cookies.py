"""Cookie profiles and channel memberships (docs/plan/access.md, Cookies and
account). A profile is the file `<name>.txt` in SAVETUBE_COOKIES_DIR. The
general profile serves every request that is not members-only. A
members-only download takes a profile whose membership in the channel a
check confirmed."""

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yt_dlp.utils import DownloadError, YoutubeDLError

from savetube import limits, logs
from savetube.config import Settings
from savetube.db.models import Channel, CookieProfile, Membership
from savetube.logs import emit
from savetube.ytdl import NotSignedIn, copy_cookies, members_only, youtube_block

log = logging.getLogger(__name__)

SUFFIX = ".txt"
# A membership result older than this is checked again by the next poll that
# lists a members-only video of the channel.
CHECK_EVERY = timedelta(hours=24)

# Reads a members-only video id with a copy of one profile's cookie file, and
# returns its info dict with formats. Raises when the account may not watch it.
Prober = Callable[[str, Path], dict[str, Any]]

# Reads the name and handle of the account a copy of one profile's cookie file
# signs in.
Reader = Callable[[Path], tuple[str, str | None]]
# A failed account read waits this long before the next try, unless the file
# changed after a read that succeeded.
ACCOUNT_RETRY = timedelta(hours=1)


def profiles(settings: Settings) -> list[str]:
    """The present profiles' names, sorted."""
    root = settings.cookies_dir
    if root is None or not root.is_dir():
        return []
    # A Secret mount also holds `..data` and timestamped dot directories.
    return sorted(
        p.name.removesuffix(SUFFIX)
        for p in root.iterdir()
        if p.name.endswith(SUFFIX) and not p.name.startswith(".") and p.is_file()
    )


def cookiefile(settings: Settings, profile: str | None, private: Path) -> Path | None:
    """A writable copy of the profile's file in `private`, None for no profile."""
    if profile is None:
        return None
    if profile not in profiles(settings):
        raise RuntimeError(f"cookie profile {profile} is not in SAVETUBE_COOKIES_DIR")
    return copy_cookies(settings.cookies_dir / f"{profile}{SUFFIX}", private)


async def record_profiles(session: AsyncSession, names: list[str], now: datetime) -> None:
    """Make `cookie_profile` hold exactly `names`. The caller commits."""
    await session.execute(delete(CookieProfile).where(CookieProfile.name.not_in(names)))
    if names:
        stmt = insert(CookieProfile).values([{"name": n, "seen_at": now} for n in names])
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[CookieProfile.name], set_={"seen_at": stmt.excluded.seen_at}
            )
        )


async def recorded_profiles(session: AsyncSession) -> list[str]:
    """The profile names the scheduler last recorded, sorted."""
    return list(await session.scalars(select(CookieProfile.name).order_by(CookieProfile.name)))


async def read_accounts(
    sessionmaker: async_sessionmaker, settings: Settings, *, reader: Reader
) -> None:
    """Read the account of each recorded profile whose file changed since the
    last successful read. One read at a time, none while a YouTube block
    pauses downloads, and none after a read YouTube refused."""
    present = profiles(settings)
    if not present:
        return
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        if limits.blocked(await limits.load(session), now) is not None:
            return
        rows = list(
            await session.scalars(
                select(CookieProfile)
                .where(CookieProfile.name.in_(present))
                .order_by(CookieProfile.name)
            )
        )
    for row in rows:
        digest = hashlib.sha256((settings.cookies_dir / f"{row.name}{SUFFIX}").read_bytes())
        digest = digest.hexdigest()
        if row.account_digest == digest:
            continue
        failed_lately = (
            row.account_digest is None
            and row.account_checked_at is not None
            and now - row.account_checked_at < ACCOUNT_RETRY
        )
        if failed_lately:
            continue
        if not await _read_account(
            sessionmaker, settings, row.name, digest, row.account_digest, reader=reader
        ):
            return


async def _read_account(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    profile: str,
    digest: str,
    previous: str | None,
    *,
    reader: Reader,
) -> bool:
    """One read of a file whose sha256 `digest` differs from the stored
    `previous`. False when YouTube refused this client, so no more follow.

    A file that replaced one read before drops the profile's membership
    results, so the next poll checks the new account at once. A failed read
    leaves `previous` null, so a later replacement keeps the results."""
    try:
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            path = cookiefile(settings, profile, Path(private))
            name, handle = await asyncio.to_thread(reader, path)
    except Exception as e:
        reason = str(e) if isinstance(e, NotSignedIn) else f"{type(e).__name__}: {e}"
        emit(
            log,
            logging.WARNING,
            "cookies.account_failed",
            profile=profile,
            reason=logs.cut(reason),
            exc_info=not isinstance(e, YoutubeDLError | NotSignedIn),
        )
        values: dict[str, Any] = dict(account_name=None, account_handle=None, account_digest=None)
        ok = not youtube_block(str(e))
    else:
        emit(log, logging.INFO, "cookies.account", profile=profile, name=name, handle=handle)
        values = dict(account_name=name, account_handle=handle, account_digest=digest)
        ok = True
    async with sessionmaker() as session:
        await session.execute(
            update(CookieProfile)
            .where(CookieProfile.name == profile)
            .values(**values, account_checked_at=datetime.now(UTC))
        )
        dropped = 0
        if previous is not None:
            result = await session.execute(delete(Membership).where(Membership.profile == profile))
            dropped = result.rowcount
        await session.commit()
    if previous is not None:
        emit(log, logging.INFO, "cookies.changed", profile=profile, memberships=dropped)
    return ok


async def general(session: AsyncSession) -> str | None:
    return (await limits.load(session)).cookie_profile


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
    listed: dict[str, str],
    *,
    prober: Prober,
) -> None:
    """Check each present profile whose result for a channel is missing or
    stale. `listed` maps a channel id to one members-only video id a poll
    listed for it. One check at a time, none while a YouTube block pauses
    downloads, and none after a check YouTube refused."""
    present = profiles(settings)
    if not listed or not present:
        return
    for channel_id, video_id in listed.items():
        async with sessionmaker() as session:
            channel = await session.get(Channel, channel_id)
            if channel is None:
                continue
            due = await _due(session, channel_id, present, datetime.now(UTC))
        for profile in due:
            async with sessionmaker() as session:
                if limits.blocked(await limits.load(session), datetime.now(UTC)) is not None:
                    return
            if not await _check(sessionmaker, settings, channel, profile, video_id, prober=prober):
                return


async def _check(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    channel: Channel,
    profile: str,
    video_id: str,
    *,
    prober: Prober,
) -> bool:
    """One check. False when YouTube refused this client, so no more follow."""
    fields = dict(channel=channel.name, profile=profile, video=video_id)
    try:
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            path = cookiefile(settings, profile, Path(private))
            info = await asyncio.to_thread(prober, video_id, path)
        is_member = bool(info.get("formats"))
        if not is_member:
            raise DownloadError("the video lists no formats")
    except Exception as e:
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
    now = datetime.now(UTC)
    row = dict(
        channel_id=channel.id,
        profile=profile,
        is_member=is_member,
        checked_at=now,
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
