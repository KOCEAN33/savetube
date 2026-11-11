"""encode_settings.motion_threshold: measured motion decides the frame rate of a
title that is not still (docs/plan/encode.md, Profiles)

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-11 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "encode_settings",
        sa.Column("motion_threshold", sa.Float(), server_default="1.0", nullable=True),
    )
    # The motion now keeps the source's rate where the null cap used to.
    op.execute("UPDATE encode_settings SET fps_cap = 30 WHERE fps_cap IS NULL")


def downgrade() -> None:
    op.drop_column("encode_settings", "motion_threshold")
