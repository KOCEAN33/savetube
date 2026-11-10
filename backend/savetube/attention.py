"""Rules the Needs attention card (docs/plan/frontend.md, Home), the job and
encode lists, and the Discord notifications (docs/plan/operations.md) share."""

from typing import Literal

from sqlalchemy import ColumnElement, Text, and_, cast, exists, or_
from sqlalchemy.orm import InstrumentedAttribute, aliased

from savetube.db.models import (
    AttentionDismissal,
    CookieProfile,
    EncodeJob,
    EncodeState,
    Job,
    JobKind,
    JobState,
    Video,
    VideoState,
)

Kind = Literal[
    "archive_failed",
    "record_failed",
    "replace_failed",
    "encode_failed",
    "held",
    "source_failing",
    "cookie_signed_out",
]
# The other kinds leave the card once fixed.
DISMISSABLE: frozenset[Kind] = frozenset(
    {"archive_failed", "record_failed", "replace_failed", "encode_failed", "held"}
)
# The kind a failed job of each job kind is dismissed under, keyed by its id.
JOB_FAILED: dict[JobKind, Kind] = {
    JobKind.ARCHIVE: "archive_failed",
    JobKind.RECORD: "record_failed",
    JobKind.REPLACE: "replace_failed",
}


def dismissed(key: InstrumentedAttribute[int], *kinds: Kind) -> ColumnElement[bool]:
    """The user dismissed the item with this key under one of the kinds. Every
    list of failures reads it, so a dismissal holds on every page."""
    return exists().where(
        AttentionDismissal.kind.in_(kinds), AttentionDismissal.key == cast(key, Text)
    )


def dismissed_job() -> ColumnElement[bool]:
    return dismissed(Job.id, *JOB_FAILED.values())


def resolved_failure() -> ColumnElement[bool]:
    """A failed or interrupted archive run a later run of its video fixed: a
    success, or for an interrupted run any later failure. The query must join
    Video on Job.video_id."""
    later = aliased(Job)
    resolved = exists().where(
        later.video_id == Job.video_id,
        later.kind == JobKind.ARCHIVE,
        or_(
            later.state == JobState.SUCCEEDED,
            and_(Job.state == JobState.INTERRUPTED, later.state == JobState.FAILED),
        ),
        later.finished_at > Job.finished_at,
    )
    return and_(
        Job.kind == JobKind.ARCHIVE,
        Job.state.in_([JobState.FAILED, JobState.INTERRUPTED]),
        # No run fixes a video YouTube no longer serves.
        or_(resolved, Video.state == VideoState.GONE),
    )


def record_failed() -> ColumnElement[bool]:
    """A failed or missed recording whose live no stored VOD holds. The query
    must join Video on Job.video_id."""
    return and_(
        Job.kind == JobKind.RECORD,
        Job.state.in_([JobState.FAILED, JobState.MISSED]),
        Video.state != VideoState.COMPLETE,
    )


def open_failure() -> ColumnElement[bool]:
    """An archive or record failure the card lists: nothing fixed it and the
    user did not dismiss it. The query must join Video on Job.video_id."""
    archive = and_(
        Job.kind == JobKind.ARCHIVE,
        Job.state.in_([JobState.FAILED, JobState.INTERRUPTED]),
        ~resolved_failure(),
    )
    return and_(or_(archive, record_failed()), ~dismissed_job())


def encode_failed() -> ColumnElement[bool]:
    """A stored video's failed encode job the user did not dismiss: the one a
    user requeues or dismisses. The query must join Video on EncodeJob.video_id."""
    return and_(
        EncodeJob.state == EncodeState.FAILED,
        Video.state == VideoState.COMPLETE,
        ~dismissed(EncodeJob.id, "encode_failed"),
    )


def signed_out() -> ColumnElement[bool]:
    """A cookie profile whose account read failed: a failed read clears the
    digest (docs/plan/access.md)."""
    return and_(
        CookieProfile.account_digest.is_(None), CookieProfile.account_checked_at.is_not(None)
    )
