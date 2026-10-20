"""source title, ignored_urls dropped

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('source', sa.Column('title', sa.Text(), nullable=True))
    # A child of a `playlists` source is disabled, never deleted, so no list of
    # deleted children is needed.
    op.drop_column('source', 'ignored_urls')


def downgrade() -> None:
    op.add_column('source', sa.Column('ignored_urls', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.drop_column('source', 'title')
