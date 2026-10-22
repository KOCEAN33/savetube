"""videos staged on a separate volume before the move into the tree

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-22 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = "'pending', 'downloading', 'complete', 'failed'"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_video_videostate'), 'video', type_='check')
    op.create_check_constraint(op.f('ck_video_videostate'), 'video', f"state IN ({_STATES}, 'staged')")


def downgrade() -> None:
    # The old code has no staging, so the next poll downloads the video again.
    op.execute("UPDATE video SET state = 'pending' WHERE state = 'staged'")
    op.drop_constraint(op.f('ck_video_videostate'), 'video', type_='check')
    op.create_check_constraint(op.f('ck_video_videostate'), 'video', f"state IN ({_STATES})")
