"""How many existing Odoo attendance records a run changed.

A run could report new punches with nothing created or closed and no way to say why (the punches
had moved an existing record). This is that count.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-06 14:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0020'
down_revision: Union[str, None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sync_run') as batch:
        batch.add_column(sa.Column('attendances_updated', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('sync_run') as batch:
        batch.drop_column('attendances_updated')
