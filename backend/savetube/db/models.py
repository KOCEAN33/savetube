import enum
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    MetaData,
    String,
    Text,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from savetube.layout import VideoType


class SourceKind(enum.StrEnum):
    TAB = "tab"
    PLAYLIST = "playlist"
    PLAYLISTS = "playlists"


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
    # The worker stopped during the run. A new run resumes it, and no attempt counts.
    INTERRUPTED = "interrupted"


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
    include_words: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    exclude_words: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    # The listing's approximate timestamp is compared, so the cutoff is coarse.
    date_after: Mapped[date | None]
    format: Mapped[str] = mapped_column(default="bv*+ba/b", server_default="bv*+ba/b")
    watch_lives: Mapped[bool] = mapped_column(default=False, server_default=false())
    # The profile picture YouTube serves, from a tab listing. Null until a scan or
    # a tab poll finds it.
    avatar_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GlobalWords(Base):
    """The one row of title words every source merges in."""

    __tablename__ = "global_words"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    include_words: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    exclude_words: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")


class DownloadSettings(Base):
    """The one row of archive download limits. Recordings ignore them."""

    __tablename__ = "download_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("max_concurrent BETWEEN 1 AND 10", name="max_concurrent"),
        CheckConstraint("rate_limit_bytes > 0", name="rate_limit_bytes"),
        CheckConstraint(
            "(window_start IS NULL AND window_end IS NULL)"
            " OR (window_start IS NOT NULL AND window_end IS NOT NULL"
            " AND window_start <> window_end)",
            name="window",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    max_concurrent: Mapped[int] = mapped_column(default=1, server_default="1")
    rate_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Local times in savetube.limits.TIMEZONE. Both set or both null.
    window_start: Mapped[time | None]
    window_end: Mapped[time | None]
    # The pause after a YouTube block, and the blocks since the last success.
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    block_count: Mapped[int] = mapped_column(default=0, server_default="0")
    # The cookie profile of every poll, live probe and non-members download.
    # Null runs them cookie-free.
    cookie_profile: Mapped[str | None] = mapped_column(Text)


class CookieProfile(Base):
    """A cookie profile present in SAVETUBE_COOKIES_DIR, by name. The
    scheduler, which mounts the profiles, records them every tick, so the web
    lists them without the mount."""

    __tablename__ = "cookie_profile"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Membership(Base):
    """Whether a cookie profile's account is a member of a channel, from the
    last check (savetube.cookies)."""

    __tablename__ = "membership"

    channel_id: Mapped[str] = mapped_column(
        ForeignKey("channel.id", ondelete="CASCADE"), primary_key=True
    )
    profile: Mapped[str] = mapped_column(Text, primary_key=True)
    is_member: Mapped[bool]
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The members-only video the check read.
    video_id: Mapped[str] = mapped_column(String(16))


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(unique=True)
    # A playlist's title from YouTube. Null for a tab or a channel's playlists.
    title: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind))
    # Null for a playlist that spans channels.
    channel_id: Mapped[str | None] = mapped_column(ForeignKey("channel.id"))
    enabled: Mapped[bool] = mapped_column(default=True)
    poll_interval_minutes: Mapped[int] = mapped_column(default=360)
    # Merged with the global, channel and parent lists (savetube.words.effective).
    include_words: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    exclude_words: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    # Off: a playlist source archives only its own channel's uploads.
    other_channels: Mapped[bool] = mapped_column(default=False, server_default=false())
    # The `playlists` source that created this playlist source.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    # Off: polls record the listing but create no videos and no archive jobs.
    # The server default is on for rows from before the flag existed.
    archiving: Mapped[bool] = mapped_column(default=False, server_default=true())
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ListingEntry(Base):
    """A video the source's last listing showed, before any filter."""

    __tablename__ = "listing_entry"

    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), primary_key=True
    )
    video_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    # No foreign key: a playlist lists uploads of channels that are never stored.
    channel_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)


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
    # The format ids an interrupted archive download chose, so its retry
    # resumes the parts in `.incoming/<video id>/`. Null without parts.
    resume_format: Mapped[str | None] = mapped_column(Text)
    # The integrity checks the stored download passed (savetube.integrity).
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_checks: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
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
    # The Kubernetes Job of a recording.
    k8s_job_name: Mapped[str | None]
    # The procrastinate job of a dispatched archive. Null while it waits for
    # the dispatcher (savetube.jobs.dispatch).
    queue_job_id: Mapped[int | None] = mapped_column(BigInteger)
    # A recording's announced start, from the waiting room. Null for a stream
    # found already live, and for an archive.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The download window's next start, for an archive job waiting outside the
    # window. Null inside it.
    deferred_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The running download's current stream: savetube.logs.progress, written by
    # the worker at most every few seconds.
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StorageSample(Base):
    __tablename__ = "storage_sample"

    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    free_bytes: Mapped[int] = mapped_column(BigInteger)
