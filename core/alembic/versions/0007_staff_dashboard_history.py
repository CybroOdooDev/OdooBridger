"""History for the staff dashboard: revenue events, service checks, more daily figures.

Revision ID: 0007_staff_dashboard_history
Revises: 0006_pending_package_change
Create Date: 2026-10-30 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007_staff_dashboard_history'
down_revision: Union[str, None] = '0006_pending_package_change'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'billing_event',
        sa.Column('account_id', sa.String(length=32), nullable=True),
        sa.Column('account_name', sa.String(length=160), nullable=True),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=8), nullable=True),
        sa.Column('mrr_before_cents', sa.Integer(), nullable=True),
        sa.Column('mrr_after_cents', sa.Integer(), nullable=True),
        sa.Column('external_id', sa.String(length=80), nullable=True),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('external_id'),
    )
    with op.batch_alter_table('billing_event', schema=None) as batch_op:
        batch_op.create_index('ix_billing_event_account_id', ['account_id'], unique=False)
        batch_op.create_index('ix_billing_event_kind_created', ['kind', 'created_at'], unique=False)

    op.create_table(
        'service_check',
        sa.Column('service_slug', sa.String(length=64), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('service_check', schema=None) as batch_op:
        batch_op.create_index('ix_service_check_slug_created', ['service_slug', 'created_at'], unique=False)

    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('first_paid_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('recorded_mrr_cents', sa.Integer(), nullable=True))
    # Accounts already paying online: count them as converted when they were created.
    op.execute("UPDATE account SET first_paid_at = created_at WHERE stripe_subscription_id IS NOT NULL")

    with op.batch_alter_table('metric_snapshot', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paying', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('open_leads', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('failed_integrations', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('outstanding', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('outstanding_cents', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('metric_snapshot', schema=None) as batch_op:
        for name in ('outstanding_cents', 'outstanding', 'failed_integrations', 'open_leads', 'paying'):
            batch_op.drop_column(name)
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.drop_column('recorded_mrr_cents')
        batch_op.drop_column('first_paid_at')
    op.drop_table('service_check')
    op.drop_table('billing_event')
