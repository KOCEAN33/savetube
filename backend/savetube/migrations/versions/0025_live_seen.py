"""live_seen: the video ids live detection probed, kept across scheduler restarts

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-25 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0025'
down_revision: str | None = '0024'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('live_seen',
    sa.Column('video_id', sa.String(length=16), nullable=False),
    sa.Column('info', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('probed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('listed_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('video_id', name=op.f('pk_live_seen'))
    )


def downgrade() -> None:
    op.drop_table('live_seen')
