"""pause one cookie profile after googlevideo refuses its downloads

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-07 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0030'
down_revision: str | None = '0029'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('cookie_profile', sa.Column('blocked_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('cookie_profile', sa.Column('block_count', sa.Integer(), server_default='0', nullable=False))
    # A profile's refusal is no fault of the video, so it gives back the attempt it took.
    op.execute(
        "UPDATE job SET state = 'interrupted'"
        " WHERE kind = 'archive' AND state = 'failed' AND log_tail ILIKE '%http error 403%'"
    )


def downgrade() -> None:
    # The jobs the upgrade turned interrupted stay interrupted, as in 0009.
    op.drop_column('cookie_profile', 'block_count')
    op.drop_column('cookie_profile', 'blocked_until')
