"""video.info without fragment lists and requested_downloads (formats.stored_info)

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-11 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LISTS = "ARRAY['formats', 'requested_formats']"


def upgrade() -> None:
    op.execute(f"""
        UPDATE video SET info = (info - 'requested_downloads') || COALESCE((
            SELECT jsonb_object_agg(key, (
                SELECT COALESCE(jsonb_agg(f - 'fragments' ORDER BY n), '[]'::jsonb)
                FROM jsonb_array_elements(info -> key) WITH ORDINALITY AS e (f, n)
            ))
            FROM unnest({_LISTS}) AS key
            WHERE jsonb_typeof(info -> key) = 'array'
        ), '{{}}'::jsonb)
        WHERE info ? 'requested_downloads'
            OR jsonb_path_exists(info, '$.formats[*].fragments')
            OR jsonb_path_exists(info, '$.requested_formats[*].fragments')
    """)


def downgrade() -> None:
    # The dropped keys live on in each video's `.info.json`. Nothing reads them
    # from the row.
    pass
