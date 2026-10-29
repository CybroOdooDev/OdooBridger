"""A package change scheduled for the next renewal.

Revision ID: 0006_pending_package_change
Revises: 0005_signup_trial_bonus
Create Date: 2026-10-29 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006_pending_package_change'
down_revision: Union[str, None] = '0005_signup_trial_bonus'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('account_service', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pending_package_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('pending_change_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key('fk_account_service_pending_package', 'service_package',
                                    ['pending_package_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('account_service', schema=None) as batch_op:
        batch_op.drop_constraint('fk_account_service_pending_package', type_='foreignkey')
        batch_op.drop_column('pending_change_at')
        batch_op.drop_column('pending_package_id')
