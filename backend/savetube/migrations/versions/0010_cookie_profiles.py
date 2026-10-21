"""cookie profiles: the general profile and channel memberships

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('membership',
    sa.Column('channel_id', sa.String(length=64), nullable=False),
    sa.Column('profile', sa.Text(), nullable=False),
    sa.Column('is_member', sa.Boolean(), nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('video_id', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channel.id'], name=op.f('fk_membership_channel_id_channel'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('channel_id', 'profile', name=op.f('pk_membership'))
    )
    op.create_table('cookie_profile',
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_cookie_profile'))
    )
    op.add_column('download_settings', sa.Column('cookie_profile', sa.Text(), nullable=True))
    # A channel's cookie switch names no profile, so it has nothing to carry over.
    op.drop_column('channel', 'use_cookies')


def downgrade() -> None:
    op.add_column('channel', sa.Column('use_cookies', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.drop_column('download_settings', 'cookie_profile')
    op.drop_table('cookie_profile')
    op.drop_table('membership')
