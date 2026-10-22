import glob
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
