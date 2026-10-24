import glob
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from savetube import formats
from savetube.db.models import Channel, Source, Video, VideoSource, VideoState
from savetube.layout import VideoType
from savetube.web.api.deps import Session, _of_channel
from savetube.web.api.schemas import _Out

router = APIRouter()


class VideoOut(_Out):
    id: str
    channel_id: str
    type: VideoType
    title: str
    upload_date: date | None
    duration: int | None
    size_bytes: int | None
    state: str
    completed_at: datetime | None
    # The integrity checks the stored download passed: size, probe, packets.
    verified_at: datetime | None
    verified_checks: list[str] | None
    format_id: str | None
    # The stored streams, named like the format watch: "AV1", "Opus".
    video_codec: str | None
    audio_codec: str | None
    video_width: int | None
    video_height: int | None
    video_fps: float | None
    video_kbps: float | None
    premium: bool | None

    @field_validator("video_codec")
    @classmethod
    def _video_name(cls, v: str | None) -> str | None:
        return formats.video_codec(v)[1] if v else None

    @field_validator("audio_codec")
    @classmethod
    def _audio_name(cls, v: str | None) -> str | None:
        return formats.audio_codec(v)[1] if v else None


# The prefixes savetube.formats.video_codec and audio_codec name.
_VIDEO_CODECS = {"av1": ("av01",), "vp9": ("vp9", "vp09"), "h264": ("avc1", "h264")}
_AUDIO_CODECS = {"opus": ("opus",), "aac": ("mp4a", "aac")}
# Lower bound of each bucket on the short side, so a 1080x1920 short is 1080.
_HEIGHTS = {"2160": 2160, "1440": 1440, "1080": 1080, "720": 720, "below": 0}


def _codec_is(
    column: InstrumentedAttribute[str | None], prefixes: tuple[str, ...]
) -> ColumnElement[bool]:
    return or_(*(column.ilike(f"{p}%") for p in prefixes))


class VideoDetail(VideoOut):
    info: dict[str, Any] | None


class SubtitleOut(BaseModel):
    lang: str
    label: str


class VideoPage(_Out):
    total: int
    items: list[VideoOut]


@router.get("/videos")
async def videos(
    session: Session,
    channel_id: str | None = None,
    # Uploaders by name. A group's member channels each have their own id
    # under the group's name, so the Videos page filters them as one.
    channel_name: str | None = None,
    # The channel page's view: also the videos the channel's sources list.
    listed_by: str | None = None,
    # Videos the source lists. A `playlists` parent lists through its children.
    source_id: int | None = None,
    type: VideoType | None = None,
    state: VideoState | None = None,
    q: str | None = None,
    # The stored video stream's codec. `other` is a known codec outside the three.
    vcodec: Literal["av1", "vp9", "h264", "other"] | None = None,
    acodec: Literal["opus", "aac"] | None = None,
    # A bucket of the stored video's short side: 2160 and up, 1440-2159, and so on.
    height: Literal["2160", "1440", "1080", "720", "below"] | None = None,
    premium: bool | None = None,
    # By upload date. A video without one sorts last either way.
    order: Literal["newest", "oldest"] = "newest",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> VideoPage:
    where = []
    if channel_id:
        where.append(Video.channel_id == channel_id)
    if channel_name:
        where.append(Video.channel_id.in_(select(Channel.id).where(Channel.name == channel_name)))
    if listed_by:
        where.append(_of_channel(listed_by))
    if source_id is not None:
        sources = select(Source.id).where(
            (Source.id == source_id) | (Source.parent_id == source_id)
        )
        listed = select(VideoSource.video_id).where(VideoSource.source_id.in_(sources))
        where.append(Video.id.in_(listed))
    if type:
        where.append(Video.type == type)
    if state:
        where.append(Video.state == state)
    if q:
        pattern = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
        where.append(or_(Video.title.ilike(pattern), Video.id == q))
    if vcodec == "other":
        known = [p for prefixes in _VIDEO_CODECS.values() for p in prefixes]
        where.append(~_codec_is(Video.video_codec, tuple(known)))
    elif vcodec:
        where.append(_codec_is(Video.video_codec, _VIDEO_CODECS[vcodec]))
    if acodec:
        where.append(_codec_is(Video.audio_codec, _AUDIO_CODECS[acodec]))
    if height:
        side = func.least(Video.video_width, Video.video_height)
        where.append(side >= _HEIGHTS[height])
        upper = [h for h in _HEIGHTS.values() if h > _HEIGHTS[height]]
        if upper:
            where.append(side < min(upper))
    if premium is not None:
        where.append(Video.premium.is_(premium))
    total = await session.scalar(select(func.count()).select_from(Video).where(*where))
    by_date = Video.upload_date.desc() if order == "newest" else Video.upload_date.asc()
    items = await session.scalars(
        select(Video)
        .where(*where)
        .order_by(by_date.nulls_last(), Video.id)
        .limit(limit)
        .offset(offset)
    )
    return VideoPage(total=total or 0, items=[VideoOut.model_validate(v) for v in items])


@router.get("/videos/{video_id}")
async def video(session: Session, video_id: str) -> VideoDetail:
    v = await session.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404)
    return VideoDetail.model_validate(v)


async def _video_dir(request: Request, session: AsyncSession, video_id: str) -> tuple[Video, Path]:
    """The resolved `<archive root>/<dir_path>` of a complete video, built from
    its row only and never outside the archive root."""
    v = await session.get(Video, video_id)
    if v is None or v.state is not VideoState.COMPLETE or not v.dir_path:
        raise HTTPException(status_code=404)
    root = request.app.state.archive_root.resolve()
    directory = (root / v.dir_path).resolve()
    if not directory.is_relative_to(root) or not directory.is_dir():
        raise HTTPException(status_code=404)
    return v, directory


async def _stored_file(request: Request, session: AsyncSession, video_id: str, ext: str) -> Path:
    """`<video dir>/<video id>.<ext>`. `ext` "" means the media's own extension
    from `info.ext`."""
    v, directory = await _video_dir(request, session, video_id)
    ext = ext or (v.info or {}).get("ext")
    if not isinstance(ext, str) or not ext:
        raise HTTPException(status_code=404)
    path = (directory / f"{v.id}.{ext}").resolve()
    if not path.is_relative_to(request.app.state.archive_root.resolve()) or not path.is_file():
        raise HTTPException(status_code=404)
    return path


async def _subtitle_files(
    request: Request, session: AsyncSession, video_id: str
) -> tuple[Video, dict[str, Path]]:
    """Language code to file for each `<video id>.<lang>.vtt` yt-dlp left in the
    video's directory. The language comes from the listing, never from a request."""
    v, directory = await _video_dir(request, session, video_id)
    files = {}
    for path in directory.glob(f"{glob.escape(v.id)}.*.vtt"):
        lang = path.name[len(v.id) + 1 : -len(".vtt")]
        resolved = path.resolve()
        if (
            lang
            and lang != "live_chat"
            and resolved.is_relative_to(directory)
            and resolved.is_file()
        ):
            files[lang] = resolved
    return v, dict(sorted(files.items()))


def _subtitle_label(info: dict[str, Any] | None, lang: str) -> str:
    tracks = ((info or {}).get("subtitles") or {}).get(lang)
    if isinstance(tracks, list):
        for track in tracks:
            if isinstance(track, dict) and isinstance(track.get("name"), str) and track["name"]:
                return track["name"]
    return lang


@router.get("/videos/{video_id}/subtitles")
async def video_subtitles(request: Request, session: Session, video_id: str) -> list[SubtitleOut]:
    v, files = await _subtitle_files(request, session, video_id)
    return [SubtitleOut(lang=lang, label=_subtitle_label(v.info, lang)) for lang in files]


@router.get("/videos/{video_id}/subtitles/{lang}")
async def video_subtitle(
    request: Request, session: Session, video_id: str, lang: str
) -> FileResponse:
    _, files = await _subtitle_files(request, session, video_id)
    if lang not in files:
        raise HTTPException(status_code=404)
    return FileResponse(files[lang], media_type="text/vtt; charset=utf-8")


# Starlette's FileResponse answers Range (206, 416, multipart) and HEAD itself.
@router.api_route("/videos/{video_id}/media", methods=["GET", "HEAD"])
async def video_media(request: Request, session: Session, video_id: str) -> FileResponse:
    path = await _stored_file(request, session, video_id, "")
    return FileResponse(path, media_type="video/x-matroska" if path.suffix == ".mkv" else None)


@router.api_route("/videos/{video_id}/audio", methods=["GET", "HEAD"])
async def video_audio(request: Request, session: Session, video_id: str) -> FileResponse:
    return FileResponse(
        await _stored_file(request, session, video_id, "opus"), media_type="audio/ogg"
    )
