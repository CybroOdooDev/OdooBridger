"""OdooBridger platform mode: link tenants and users to core accounts

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str, column: str) -> bool:
    return column in {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    with op.batch_alter_table('tenant', schema=None) as batch_op:
        if not _has('tenant', 'platform_account_id'):
            batch_op.add_column(sa.Column('platform_account_id', sa.String(length=32), nullable=True))
            batch_op.create_index(batch_op.f('ix_tenant_platform_account_id'), ['platform_account_id'], unique=True)
        if not _has('tenant', 'platform_state'):
            batch_op.add_column(sa.Column('platform_state', sa.JSON(), nullable=True))
    with op.batch_alter_table('app_user', schema=None) as batch_op:
        if not _has('app_user', 'platform_user_id'):
            batch_op.add_column(sa.Column('platform_user_id', sa.String(length=32), nullable=True))
            batch_op.create_index(batch_op.f('ix_app_user_platform_user_id'), ['platform_user_id'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('app_user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_app_user_platform_user_id'))
        batch_op.drop_column('platform_user_id')
    with op.batch_alter_table('tenant', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenant_platform_account_id'))
        batch_op.drop_column('platform_state')
        batch_op.drop_column('platform_account_id')
