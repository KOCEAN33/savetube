"""The signed-in user, global words, staging, download settings and cookie profiles."""

import logging
from datetime import UTC, datetime, time
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube import cookies, limits, logs
from savetube.db.models import DownloadSettings, GlobalWords, Video, VideoState
from savetube.limits import TIMEZONE
from savetube.logs import emit
from savetube.web.api.deps import Session, _normalize, log
from savetube.web.api.schemas import WordsIn, _Out
from savetube.web.auth import CurrentUser, User

router = APIRouter()


@router.get("/me")
async def me(user: CurrentUser) -> User:
    return user


@router.get("/words")
async def global_words(session: Session) -> WordsIn:
    row = await session.get(GlobalWords, 1)
    if row is None:
        return WordsIn()
    return WordsIn(include_words=row.include_words, exclude_words=row.exclude_words)


@router.put("/words")
async def replace_global_words(session: Session, body: WordsIn) -> WordsIn:
    _normalize(body)
    row = await session.get(GlobalWords, 1)
    if row is None:
        row = GlobalWords(id=1)
        session.add(row)
    row.include_words, row.exclude_words = body.include_words, body.exclude_words
    await session.commit()
    emit(
        log,
        logging.INFO,
        "words.update",
        scope="global",
        include=len(body.include_words),
        exclude=len(body.exclude_words),
    )
    return body


class StagedOut(BaseModel):
    count: int
    bytes: int
    # A Move now the scheduler has not taken yet.
    move_requested: bool


@router.get("/staging")
async def staging(session: Session) -> StagedOut:
    """The videos waiting on staging for the move into the tree."""
    count, size = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Video.size_bytes), 0)).where(
                Video.state == VideoState.STAGED
            )
        )
    ).one()
    requested = (await limits.load(session)).move_requested_at is not None
    return StagedOut(count=count, bytes=size, move_requested=requested)


@router.post("/staging/move")
async def move_staging(session: Session) -> StagedOut:
    """Move every staged video now. The scheduler does it on its next check,
    within a minute (savetube.staging)."""
    row = await session.get(DownloadSettings, 1)
    if row is None:
        row = DownloadSettings(id=1)
        session.add(row)
    row.move_requested_at = datetime.now(UTC)
    await session.commit()
    emit(log, logging.INFO, "staging.move_requested")
    return await staging(session)


_HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class DownloadSettingsIn(BaseModel):
    max_concurrent: Annotated[int, Field(ge=1, le=10)] = 1
    rate_limit_bytes: Annotated[int | None, Field(gt=0)] = None
    window_start: _HHMM | None = None
    window_end: _HHMM | None = None
    # Off keeps the times and ignores them.
    window_enabled: bool = False
    # Archive downloads per Asia/Seoul day. Null for no cap.
    daily_limit: Annotated[int | None, Field(ge=1)] = None
    # A present cookie profile's name. Null runs polls, live probes and
    # non-members downloads cookie-free.
    cookie_profile: str | None = None

    @model_validator(mode="after")
    def _window(self) -> DownloadSettingsIn:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("set both window_start and window_end, or neither")
        if self.window_start is not None and self.window_start == self.window_end:
            raise ValueError("window_start and window_end must differ")
        if self.window_enabled and self.window_start is None:
            raise ValueError("set window_start and window_end to enable the window")
        return self


class DownloadSettingsOut(DownloadSettingsIn):
    timezone: str
    # Set by a YouTube block, read-only here (savetube.jobs).
    blocked_until: datetime | None = None
    block_count: int = 0
    # Set through PUT /api/download-settings/paused only.
    paused: bool = False
    # Archive downloads succeeded today in Asia/Seoul, plus running ones.
    downloaded_today: int = 0


class PausedIn(BaseModel):
    paused: bool


def _hhmm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t is not None else None


async def _settings_out(session: AsyncSession, row: DownloadSettings) -> DownloadSettingsOut:
    return DownloadSettingsOut(
        max_concurrent=row.max_concurrent,
        rate_limit_bytes=row.rate_limit_bytes,
        window_start=_hhmm(row.window_start),
        window_end=_hhmm(row.window_end),
        window_enabled=row.window_enabled,
        daily_limit=row.daily_limit,
        cookie_profile=row.cookie_profile,
        timezone=TIMEZONE.key,
        blocked_until=row.blocked_until,
        block_count=row.block_count,
        paused=row.paused,
        downloaded_today=await limits.downloaded_today(session, datetime.now(UTC)),
    )


@router.get("/download-settings")
async def download_settings(session: Session) -> DownloadSettingsOut:
    return await _settings_out(session, await limits.load(session))


@router.put("/download-settings")
async def replace_download_settings(
    session: Session, body: DownloadSettingsIn
) -> DownloadSettingsOut:
    """The scheduler's next dispatch applies the new limits to waiting jobs."""
    present = await cookies.recorded_profiles(session)
    if body.cookie_profile is not None and body.cookie_profile not in present:
        raise HTTPException(
            status_code=422, detail=f"no cookie profile named {body.cookie_profile}"
        )
    row = await session.get(DownloadSettings, 1)
    if row is None:
        row = DownloadSettings(id=1)
        session.add(row)
    row.max_concurrent = body.max_concurrent
    row.rate_limit_bytes = body.rate_limit_bytes
    row.window_start = time.fromisoformat(body.window_start) if body.window_start else None
    row.window_end = time.fromisoformat(body.window_end) if body.window_end else None
    row.window_enabled = body.window_enabled
    row.daily_limit = body.daily_limit
    row.cookie_profile = body.cookie_profile
    await session.commit()
    emit(
        log,
        logging.INFO,
        "settings.download",
        max_concurrent=body.max_concurrent,
        rate_limit=f"{logs.human_bytes(body.rate_limit_bytes)}/s"
        if body.rate_limit_bytes
        else "none",
        window=limits.window_label(row) or "none",
        daily_limit=body.daily_limit or "none",
        cookie_profile=body.cookie_profile or "none",
    )
    return await _settings_out(session, row)


@router.put("/download-settings/paused")
async def pause_downloads(session: Session, body: PausedIn) -> DownloadSettingsOut:
    """Pause or resume archive downloads. The next dispatch, and every running
    download within PAUSE_POLL_SECONDS, applies it. Recordings ignore it."""
    row = await session.get(DownloadSettings, 1)
    if row is None:
        row = DownloadSettings(id=1)
        session.add(row)
    changed = row.paused != body.paused
    row.paused = body.paused
    running = await limits.running(session)
    await session.commit()
    if changed:
        emit(
            log,
            logging.INFO,
            "dispatch.paused" if body.paused else "dispatch.resumed",
            running=running,
        )
    return await _settings_out(session, row)


class CookieProfileOut(_Out):
    name: str


@router.get("/cookie-profiles")
async def cookie_profiles(session: Session) -> list[CookieProfileOut]:
    """The profiles the scheduler last found in SAVETUBE_COOKIES_DIR, by name.
    The web pod does not mount them."""
    return [CookieProfileOut(name=n) for n in await cookies.recorded_profiles(session)]
