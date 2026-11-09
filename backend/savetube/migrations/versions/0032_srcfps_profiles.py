"""encode_settings: the -srcfps profiles keep a 60 fps source's rate (docs/plan/encode.md)

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-09 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE encode_settings SET profile = profile || '-srcfps'"
        " WHERE profile IN ('vtuber-av1-hq-cq46', 'vtuber-av1-uhq-cq46')"
    )
    op.alter_column("encode_settings", "profile", server_default="vtuber-av1-hq-cq46-srcfps")


def downgrade() -> None:
    op.execute(
        "UPDATE encode_settings SET profile = left(profile, -length('-srcfps'))"
        " WHERE profile LIKE '%-srcfps'"
    )
    op.alter_column("encode_settings", "profile", server_default="vtuber-av1-uhq-cq46")
