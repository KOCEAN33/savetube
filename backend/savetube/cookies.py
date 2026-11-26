"""Cookie profiles. A profile is the file `<name>.txt` in SAVETUBE_COOKIES_DIR. The
general profile serves every request that is not members-only. savetube.membership
picks the profile a members-only request takes."""

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
from yt_dlp.utils import YoutubeDLError

from savetube import limits, logs
from savetube.config import Settings
from savetube.db.models import CookieProfile, Membership, Video
from savetube.logs import emit
from savetube.ytdl import NotSignedIn, copy_cookies, youtube_block

log = logging.getLogger(__name__)

SUFFIX = ".txt"
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
    if previous is not None or values["account_digest"] is not None:
        # New content may hold a session googlevideo serves.
        values.update(blocked_until=None, block_count=0)
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
            # The new account may hold another membership level.
            await session.execute(
                update(Video).where(Video.members_level.is_not(None)).values(members_level=None)
            )
        await session.commit()
    if previous is not None:
        emit(log, logging.INFO, "cookies.changed", profile=profile, memberships=dropped)
    return ok


async def general(session: AsyncSession) -> str | None:
    return (await limits.load(session)).cookie_profile
