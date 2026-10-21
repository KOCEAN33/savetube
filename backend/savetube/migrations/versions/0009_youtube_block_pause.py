"""pause archive downloads after a YouTube block

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('download_settings', sa.Column('blocked_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('download_settings', sa.Column('block_count', sa.Integer(), server_default='0', nullable=False))
    # A bot check is no fault of the video, so it gives back the attempt it took.
    op.execute(
        "UPDATE job SET state = 'interrupted'"
        " WHERE kind = 'archive' AND state = 'failed' AND log_tail ILIKE '%not a bot%'"
    )


def downgrade() -> None:
    # The jobs the upgrade turned interrupted stay interrupted. 0008's
    # downgrade turns them cancelled, which counts no attempt either.
    op.drop_column('download_settings', 'block_count')
    op.drop_column('download_settings', 'blocked_until')
