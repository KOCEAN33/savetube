"""Dependencies and helpers the route modules share."""

import logging
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from savetube import words
from savetube.db.models import Channel, Source, SourceKind, Video, VideoSource, VideoState
from savetube.sources import InvalidSourceUrl, Tab
from savetube.sources import parse as parse_source_url

# One logger for the whole API, named as when it was a single module.
log = logging.getLogger("savetube.web.api")


async def get_session(request: Request):
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]

CSRF_HEADER = "X-Requested-With"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_csrf_header(request: Request) -> None:
    """A cross-origin page cannot send a custom header without a CORS preflight,
    which this app never answers. SameSite=lax alone still lets a sibling
    subdomain POST with the session cookie."""
    if request.method not in _SAFE_METHODS and CSRF_HEADER.lower() not in request.headers:
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")


_size = func.coalesce(func.sum(Video.size_bytes), 0)
# Statistics count what sits in the tree, not pending or failed downloads.
_stored = Video.state == VideoState.COMPLETE
# A channel the user added: at least one source names it.
_tracked = select(Source.id).where(Source.channel_id == Channel.id).exists()


def _of_channel(channel_id: str | ColumnElement[str]) -> ColumnElement[bool]:
    """Videos the channel uploaded or one of its sources lists. A collaboration
    on the channel's tab is stored under the uploader the video's info names."""
    listed = (
        select(VideoSource.video_id)
        .join(Source, Source.id == VideoSource.source_id)
        .where(Source.channel_id == channel_id)
    )
    return (Video.channel_id == channel_id) | Video.id.in_(listed)


def _logged(fields: dict[str, Any]) -> dict[str, Any]:
    """Settings fields for a log line: a word list as its count."""
    out = {}
    for k, v in fields.items():
        if isinstance(v, list):
            out[k] = len(v)
        elif isinstance(v, bool):
            out[k] = "on" if v else "off"
        else:
            out[k] = v
    return out


def _normalize(body: BaseModel) -> None:
    for field in ("include_words", "exclude_words"):
        value = getattr(body, field)
        if value is not None:
            setattr(body, field, words.normalize_words(value))


def _channel_base(url: str) -> str:
    """`https://www.youtube.com/<channel>` for a channel URL, with or without a tab."""
    try:
        [parsed, *_] = parse_source_url(url, [Tab.VIDEOS])
    except InvalidSourceUrl as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if parsed.kind is SourceKind.PLAYLIST:
        raise HTTPException(status_code=422, detail="not a channel URL")
    return parsed.url.rsplit("/", 1)[0]
