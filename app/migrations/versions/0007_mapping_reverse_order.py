"""mapping reverse order

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17 02:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('mappings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reverse_order', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('mappings', schema=None) as batch_op:
        batch_op.drop_column('reverse_order')
