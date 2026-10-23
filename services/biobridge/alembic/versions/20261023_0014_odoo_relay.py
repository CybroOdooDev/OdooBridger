"""Platform mode: an Odoo reached through the customer's OdooBridger Agent

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-23 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('odoo_connection')}
    if 'relay_agent_id' not in columns:
        with op.batch_alter_table('odoo_connection', schema=None) as batch_op:
            batch_op.add_column(sa.Column('relay_agent_id', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('odoo_connection', schema=None) as batch_op:
        batch_op.drop_column('relay_agent_id')
