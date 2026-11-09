"""attention_dismissal: Needs attention items the user dismissed (docs/plan/frontend.md, Home)

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-09 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attention_dismissal",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "dismissed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("kind", "key", name=op.f("pk_attention_dismissal")),
    )


def downgrade() -> None:
    op.drop_table("attention_dismissal")
