"""manual probes, manual replace jobs, originals kept 7 days after a manual swap

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-25 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0023'
down_revision: str | None = '0022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column('format_watch', 'started_at', nullable=True)
    op.add_column(
        'format_watch', sa.Column('original_until', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'job', sa.Column('manual', sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    # A watch's end deleted its original, so no original is kept any more.
    op.execute("UPDATE format_watch SET original_path = NULL WHERE ended_at IS NOT NULL")
    # A pick refuses while a replace waits or runs, so the active replace of a
    # picked watch is the pick.
    op.execute(
        "UPDATE job SET manual = true FROM format_watch w"
        " WHERE job.kind = 'replace' AND job.state IN ('queued', 'running')"
        " AND w.video_id = job.video_id AND w.manual"
    )


def downgrade() -> None:
    op.drop_column('job', 'manual')
    op.drop_column('format_watch', 'original_until')
    op.execute("DELETE FROM format_watch WHERE started_at IS NULL")
    op.alter_column('format_watch', 'started_at', nullable=False)
