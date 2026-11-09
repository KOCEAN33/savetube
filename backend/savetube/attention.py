"""Rules the Needs attention card (docs/plan/frontend.md, Home) and the
Discord notifications (docs/plan/operations.md) share."""

from sqlalchemy import ColumnElement, and_

from savetube.db.models import CookieProfile, Job, JobKind, JobState, Video, VideoState


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
