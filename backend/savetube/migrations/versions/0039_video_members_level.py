"""video.members_level: the membership level a members-only video asks for

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-27 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0039'
down_revision: str | None = '0038'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('video', sa.Column('members_level', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('video', 'members_level')
