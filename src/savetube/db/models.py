import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from savetube.layout import VideoType


class SourceKind(enum.StrEnum):
    TAB = "tab"
    PLAYLIST = "playlist"


class VideoState(enum.StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"


class JobKind(enum.StrEnum):
    ARCHIVE = "archive"
    RECORD = "record"


class JobState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MISSED = "missed"
    CANCELLED = "cancelled"


class MissReason(enum.StrEnum):
    DVR_OFF = "dvr_off"
    ENDED_BEFORE_DETECTION = "ended_before_detection"
    RECORDER_FAILED = "recorder_failed"


def _enum(cls: type[enum.StrEnum]) -> Enum:
    # VARCHAR plus CHECK instead of a Postgres enum type, so adding a value is
    # a constraint swap rather than ALTER TYPE.
    return Enum(
        cls,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Channel(Base):
    __tablename__ = "channel"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    handle: Mapped[str | None]
    name: Mapped[str]
    # Fixed at first add. A later rename on YouTube changes `name` only.
    dir_name: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Rule(Base):
    __tablename__ = "rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    types: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    date_after: Mapped[date | None]
    format: Mapped[str] = mapped_column(default="bv*+ba/b")
    use_cookies: Mapped[bool] = mapped_column(default=False)
    watch_lives: Mapped[bool] = mapped_column(default=False)
    max_concurrent: Mapped[int] = mapped_column(default=1)


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind))
    # Null for a playlist that spans channels.
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channel.id"))
    enabled: Mapped[bool] = mapped_column(default=True)
    poll_interval_minutes: Mapped[int] = mapped_column(default=360)
    rule_id: Mapped[int] = mapped_column(ForeignKey("rule.id"))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Video(Base):
    __tablename__ = "video"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channel.id"), index=True)
    type: Mapped[VideoType] = mapped_column(_enum(VideoType))
    title: Mapped[str]
    upload_date: Mapped[date | None]
    duration: Mapped[int | None]
    live_status: Mapped[str | None]
    availability: Mapped[str | None]
    format_id: Mapped[str | None]
    # Relative to the archive root.
    dir_path: Mapped[str | None]
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[VideoState] = mapped_column(_enum(VideoState), default=VideoState.PENDING)
    info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VideoSource(Base):
    __tablename__ = "video_source"

    video_id: Mapped[str] = mapped_column(
        ForeignKey("video.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), primary_key=True
    )


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[JobKind] = mapped_column(_enum(JobKind))
    state: Mapped[JobState] = mapped_column(_enum(JobState), default=JobState.QUEUED, index=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey("video.id"), index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    miss_reason: Mapped[MissReason | None] = mapped_column(_enum(MissReason))
    log_tail: Mapped[str | None] = mapped_column(Text)
    # The Kubernetes Job of a recording, or the procrastinate job of an archive.
    k8s_job_name: Mapped[str | None]
    queue_job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StorageSample(Base):
    __tablename__ = "storage_sample"

    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    free_bytes: Mapped[int] = mapped_column(BigInteger)
