"""Remember which Odoo database the tenant's employee links belong to.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-28 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tenant') as batch:
        batch.add_column(sa.Column('odoo_identity', sa.String(length=300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tenant') as batch:
        batch.drop_column('odoo_identity')
