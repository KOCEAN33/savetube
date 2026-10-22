"""a channel's switch to keep every video its sources list, whatever the title words

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-22 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: str | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('channel', sa.Column('keep_all', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('channel', 'keep_all')
