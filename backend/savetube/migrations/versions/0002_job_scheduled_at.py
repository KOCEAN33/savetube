"""job scheduled_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('job', sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('job', 'scheduled_at')
