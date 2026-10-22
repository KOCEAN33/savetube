"""the YouTube account each cookie profile's file holds, and the file digest it was read from

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-22 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('cookie_profile', sa.Column('account_name', sa.Text(), nullable=True))
    op.add_column('cookie_profile', sa.Column('account_handle', sa.Text(), nullable=True))
    op.add_column('cookie_profile', sa.Column('account_digest', sa.Text(), nullable=True))
    op.add_column('cookie_profile', sa.Column('account_checked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('cookie_profile', 'account_checked_at')
    op.drop_column('cookie_profile', 'account_digest')
    op.drop_column('cookie_profile', 'account_handle')
    op.drop_column('cookie_profile', 'account_name')
