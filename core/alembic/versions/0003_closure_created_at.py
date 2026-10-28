"""Keep a deleted account's creation date on its closure record.

The staff overview's customer history needs it: without it a deleted account
can't be placed in the months it existed. Older closures stay empty and count
only from the month they closed.

Revision ID: 0003_closure_created_at
Revises: 0002_drop_relay_agent
Create Date: 2026-10-28 05:59:14.770912
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003_closure_created_at'
down_revision: Union[str, None] = '0002_drop_relay_agent'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('account_closure', schema=None) as batch_op:
        batch_op.add_column(sa.Column('account_created_at', sa.DateTime(timezone=True), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('account_closure', schema=None) as batch_op:
        batch_op.drop_column('account_created_at')

