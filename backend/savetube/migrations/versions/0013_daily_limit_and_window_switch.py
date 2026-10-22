"""a daily cap on archive downloads, and a switch for the download window

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-22 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('download_settings', sa.Column('window_enabled', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('download_settings', sa.Column('daily_limit', sa.Integer(), nullable=True))
    # A window with both times applied before the switch existed.
    op.execute("UPDATE download_settings SET window_enabled = true WHERE window_start IS NOT NULL AND window_end IS NOT NULL")
    op.create_check_constraint(op.f('ck_download_settings_window_enabled'), 'download_settings', 'NOT window_enabled OR window_start IS NOT NULL')
    op.create_check_constraint(op.f('ck_download_settings_daily_limit'), 'download_settings', 'daily_limit >= 1')


def downgrade() -> None:
    op.drop_constraint(op.f('ck_download_settings_daily_limit'), 'download_settings', type_='check')
    op.drop_constraint(op.f('ck_download_settings_window_enabled'), 'download_settings', type_='check')
    op.drop_column('download_settings', 'daily_limit')
    op.drop_column('download_settings', 'window_enabled')
