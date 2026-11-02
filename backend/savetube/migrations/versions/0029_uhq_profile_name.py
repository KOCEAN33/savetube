"""encode_settings: vtuber-av1-cq46 renamed vtuber-av1-uhq-cq46 (docs/plan/encode.md)

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-03 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0029'
down_revision: str | None = '0028'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# encode_job rows keep the old name: their job files on the share carry it,
# and the result check compares the two.
def upgrade() -> None:
    op.execute(
        "UPDATE encode_settings SET profile = 'vtuber-av1-uhq-cq46'"
        " WHERE profile = 'vtuber-av1-cq46'"
    )
    op.alter_column('encode_settings', 'profile', server_default='vtuber-av1-uhq-cq46')


def downgrade() -> None:
    op.execute(
        "UPDATE encode_settings SET profile = 'vtuber-av1-cq46'"
        " WHERE profile = 'vtuber-av1-uhq-cq46'"
    )
    op.alter_column('encode_settings', 'profile', server_default='vtuber-av1-cq46')
