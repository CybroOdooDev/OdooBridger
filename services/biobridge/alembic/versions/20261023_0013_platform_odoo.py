"""OdooBridger platform mode: the Odoo connection comes from the core

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-23 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str, column: str) -> bool:
    return column in {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    with op.batch_alter_table('odoo_connection', schema=None) as batch_op:
        if not _has('odoo_connection', 'platform_connection_id'):
            batch_op.add_column(sa.Column('platform_connection_id', sa.String(length=32), nullable=True))
            batch_op.create_index(batch_op.f('ix_odoo_connection_platform_connection_id'),
                                  ['platform_connection_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('odoo_connection', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_odoo_connection_platform_connection_id'))
        batch_op.drop_column('platform_connection_id')
