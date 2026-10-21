"""set failed videos with a queued archive job back to pending

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The poll queued a retry without resetting the video, so it read failed
    # while its job waited.
    op.execute(
        "UPDATE video SET state = 'pending'"
        " WHERE state = 'failed' AND EXISTS ("
        "SELECT 1 FROM job WHERE job.video_id = video.id"
        " AND job.kind = 'archive' AND job.state = 'queued')"
    )


def downgrade() -> None:
    # A pending video is right for the old code too.
    pass
