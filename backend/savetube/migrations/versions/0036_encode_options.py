"""encode_settings: encode options replace the profile name, encode_job keeps the
options it sent (docs/plan/encode.md)

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-11 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STILL_WORDS = (
    "'{歌枠,うたわく,ASMR,雑談,karaoke,\"singing stream\",\"free talk\",freetalk}'::text[]"
)


def upgrade() -> None:
    op.add_column(
        "encode_settings", sa.Column("tune", sa.Text(), server_default="hq", nullable=False)
    )
    op.add_column(
        "encode_settings", sa.Column("cq", sa.Integer(), server_default="46", nullable=False)
    )
    op.add_column("encode_settings", sa.Column("fps_cap", sa.Float(), nullable=True))
    op.add_column(
        "encode_settings",
        sa.Column("still_fps_cap", sa.Float(), server_default="30", nullable=True),
    )
    op.add_column(
        "encode_settings",
        sa.Column(
            "still_words", ARRAY(sa.Text()), server_default=sa.text(STILL_WORDS), nullable=False
        ),
    )
    op.add_column(
        "encode_settings",
        sa.Column(
            "still_exclude_words",
            ARRAY(sa.Text()),
            server_default=sa.text("'{3D}'::text[]"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE encode_settings SET"
        " tune = CASE WHEN profile LIKE '%-uhq-%' THEN 'uhq' ELSE 'hq' END,"
        " fps_cap = CASE WHEN profile LIKE '%-srcfps' THEN NULL ELSE 30 END,"
        " cq = 46"
    )
    op.drop_column("encode_settings", "profile")
    op.add_column("encode_job", sa.Column("options", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("encode_job", "options")
    op.add_column(
        "encode_settings",
        sa.Column(
            "profile", sa.Text(), server_default="vtuber-av1-hq-cq46-srcfps", nullable=False
        ),
    )
    # The named profiles hold cq 46 only, and a cap of 30 or none.
    op.execute(
        "UPDATE encode_settings SET profile = 'vtuber-av1-'"
        " || CASE WHEN tune = 'uhq' THEN 'uhq' ELSE 'hq' END || '-cq46'"
        " || CASE WHEN fps_cap IS NULL THEN '-srcfps' ELSE '' END"
    )
    for column in ("still_exclude_words", "still_words", "still_fps_cap", "fps_cap", "cq", "tune"):
        op.drop_column("encode_settings", column)
