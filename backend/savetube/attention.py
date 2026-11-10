"""Rules the Needs attention card (docs/plan/frontend.md, Home) and the
Discord notifications (docs/plan/operations.md) share."""

from sqlalchemy import ColumnElement, Text, and_, cast, exists

from savetube.db.models import (
    AttentionDismissal,
    CookieProfile,
    Job,
    JobKind,
    JobState,
    Video,
    VideoState,
)


def dismissed(key: ColumnElement, *kinds: str) -> ColumnElement[bool]:
    """The user dismissed the item with this key under one of the kinds. Every
    list of failures reads it, so a dismissal holds on every page."""
    return exists().where(
        AttentionDismissal.kind.in_(kinds), AttentionDismissal.key == cast(key, Text)
    )


def record_failed() -> ColumnElement[bool]:
    """A failed or missed recording whose live no stored VOD holds. The query
    must join Video on Job.video_id."""
    return and_(
        Job.kind == JobKind.RECORD,
        Job.state.in_([JobState.FAILED, JobState.MISSED]),
        Video.state != VideoState.COMPLETE,
    )


def signed_out() -> ColumnElement[bool]:
    """A cookie profile whose account read failed: a failed read clears the
    digest (docs/plan/access.md)."""
    return and_(
        CookieProfile.account_digest.is_(None), CookieProfile.account_checked_at.is_not(None)
    )
