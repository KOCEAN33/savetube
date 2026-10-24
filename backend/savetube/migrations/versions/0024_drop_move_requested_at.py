"""drop the Move now flag: the web asks the scheduler's API instead

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-25 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0024'
down_revision: str | None = '0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column('download_settings', 'move_requested_at')


def downgrade() -> None:
    op.add_column('download_settings', sa.Column('move_requested_at', sa.DateTime(timezone=True), nullable=True))
