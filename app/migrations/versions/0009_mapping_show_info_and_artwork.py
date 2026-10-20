"""mapping show info and artwork

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20 12:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('mappings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel_name', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('source_description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('show_info_checked_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('artwork', sa.JSON(), server_default='{}', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('mappings', schema=None) as batch_op:
        batch_op.drop_column('artwork')
        batch_op.drop_column('show_info_checked_at')
        batch_op.drop_column('source_description')
        batch_op.drop_column('channel_name')
