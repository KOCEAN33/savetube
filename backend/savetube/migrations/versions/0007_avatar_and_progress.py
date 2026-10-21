"""channel avatar, job progress

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing channels get it on their next tab poll.
    op.add_column('channel', sa.Column('avatar_url', sa.Text(), nullable=True))
    op.add_column('job', sa.Column('progress', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('job', 'progress')
    op.drop_column('channel', 'avatar_url')
