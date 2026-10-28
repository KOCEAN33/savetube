from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from savetube.db.models import Channel, StorageSample, Video
from savetube.layout import VideoType
from savetube.web.api.deps import Session, _channel_videos, _size, _stored, _tracked
from savetube.web.api.schemas import _Out

router = APIRouter()


class TypeStat(_Out):
    type: VideoType
    bytes: int
    count: int


class ChannelStat(_Out):
    channel_id: str
    name: str
    bytes: int
    count: int


class Summary(_Out):
    total_bytes: int
    video_count: int
    duration_seconds: int
    free_bytes: int | None
    by_type: list[TypeStat]
    by_channel: list[ChannelStat]


class DailyStat(_Out):
    day: date
    bytes: int
    count: int


@router.get("/stats/summary")
async def summary(session: Session) -> Summary:
    total_bytes, video_count, duration = (
        await session.execute(
            select(_size, func.count(), func.coalesce(func.sum(Video.duration), 0)).where(_stored)
        )
    ).one()
    by_type = await session.execute(
        select(Video.type, _size.label("bytes"), func.count().label("count"))
        .where(_stored)
        .group_by(Video.type)
    )
    pairs = _channel_videos()
    by_channel = await session.execute(
        select(
            Channel.id.label("channel_id"),
            Channel.name,
            _size.label("bytes"),
            func.count(Video.id).label("count"),
        )
        # The channel list's rule: tracked channels only, and a video counts
        # when the channel uploaded it or one of its sources lists it.
        .join(pairs, pairs.c.channel_id == Channel.id)
        .join(Video, Video.id == pairs.c.video_id)
        .where(_tracked)
        .group_by(Channel.id)
        .order_by(_size.desc())
    )
    free = await session.scalar(
        select(StorageSample.free_bytes).order_by(StorageSample.sampled_at.desc()).limit(1)
    )
    return Summary(
        total_bytes=total_bytes,
        video_count=video_count,
        duration_seconds=duration,
        free_bytes=free,
        by_type=[TypeStat.model_validate(r) for r in by_type],
        by_channel=[ChannelStat.model_validate(r) for r in by_channel],
    )


@router.get("/stats/daily")
async def daily(
    session: Session, days: Annotated[int, Query(ge=1, le=3650)] = 90
) -> list[DailyStat]:
    day = func.date_trunc("day", Video.completed_at).label("day")
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await session.execute(
        select(day, _size.label("bytes"), func.count().label("count"))
        .where(_stored, Video.completed_at >= since)
        .group_by(day)
        .order_by(day)
    )
    return [DailyStat(day=r.day.date(), bytes=r.bytes, count=r.count) for r in rows]
