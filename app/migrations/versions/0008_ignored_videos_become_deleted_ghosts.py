"""ignored videos become deleted ghosts

Deleting a video now keeps its row with status "deleted" (shown as a restorable ghost), so the separate
ignored_videos list is folded into videos: each ignored entry becomes a ghost at the end of its mapping.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17 02:30:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    ignored = conn.execute(sa.text(
        "SELECT i.mapping_id, i.youtube_id, i.title, i.ignored_at FROM ignored_videos i "
        "WHERE NOT EXISTS (SELECT 1 FROM videos v WHERE v.mapping_id = i.mapping_id AND v.youtube_id = i.youtube_id) "
        "ORDER BY i.mapping_id, i.ignored_at, i.id"
    )).all()
    next_position: dict[int, int] = {}
    for mapping_id, youtube_id, title, ignored_at in ignored:
        if mapping_id not in next_position:
            last = conn.execute(sa.text("SELECT MAX(position) FROM videos WHERE mapping_id = :m"), {"m": mapping_id})
            next_position[mapping_id] = (last.scalar() or 0) + 1
        conn.execute(sa.text(
            "INSERT INTO videos (mapping_id, youtube_id, yt_title, position, user_edited, status, attempts, added_at, source) "
            "VALUES (:m, :y, :t, :p, 0, 'deleted', 0, :at, 'sync')"
        ), {"m": mapping_id, "y": youtube_id, "t": title or "", "p": next_position[mapping_id], "at": ignored_at})
        next_position[mapping_id] += 1
    op.drop_table('ignored_videos')


def downgrade() -> None:
    op.create_table(
        'ignored_videos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('mapping_id', sa.Integer(), nullable=False),
        sa.Column('youtube_id', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('ignored_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['mapping_id'], ['mappings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mapping_id', 'youtube_id'),
    )
    with op.batch_alter_table('ignored_videos', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ignored_videos_mapping_id'), ['mapping_id'], unique=False)
    op.execute(
        "INSERT INTO ignored_videos (mapping_id, youtube_id, title, ignored_at) "
        "SELECT mapping_id, youtube_id, COALESCE(NULLIF(custom_title, ''), yt_title), added_at FROM videos "
        "WHERE status = 'deleted'"
    )
    op.execute("DELETE FROM videos WHERE status = 'deleted'")
