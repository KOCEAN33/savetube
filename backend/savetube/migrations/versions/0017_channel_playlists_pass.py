"""a channel's switch to keep every video its tracked playlists list, whatever the include words

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-22 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('channel', sa.Column('playlists_pass', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('channel', 'playlists_pass')
