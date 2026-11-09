"""notification: items the scheduler posted to Discord (docs/plan/operations.md)

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-09 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("kind", "key", name=op.f("pk_notification")),
    )


def downgrade() -> None:
    op.drop_table("notification")
