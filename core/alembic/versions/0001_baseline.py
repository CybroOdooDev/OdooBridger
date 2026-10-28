"""Baseline: the core's schema as it was when migrations were introduced (Oct 28, 2026).

A database that predates migrations is adopted (topped up and stamped at this
revision) by obcore.migrations, never re-created.

Revision ID: 0001_baseline
Revises: 
Create Date: 2026-10-28 05:20:57.350994
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001_baseline'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('account',
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('country', sa.String(length=80), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('suspension_reason', sa.String(length=200), nullable=True),
    sa.Column('stripe_customer_id', sa.String(length=64), nullable=True),
    sa.Column('stripe_subscription_id', sa.String(length=64), nullable=True),
    sa.Column('billing_interval', sa.String(length=5), server_default='month', nullable=False),
    sa.Column('stripe_status', sa.String(length=24), nullable=True),
    sa.Column('renews_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_at_period_end', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('lead_id', sa.String(length=32), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_account_slug'), ['slug'], unique=True)
        batch_op.create_index(batch_op.f('ix_account_stripe_customer_id'), ['stripe_customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_account_stripe_subscription_id'), ['stripe_subscription_id'], unique=False)

    op.create_table('account_closure',
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('account_name', sa.String(length=160), nullable=False),
    sa.Column('owner_email', sa.String(length=254), nullable=True),
    sa.Column('services', sa.JSON(), nullable=False),
    sa.Column('closed_by', sa.String(length=280), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('account_closure', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_account_closure_account_id'), ['account_id'], unique=False)

    op.create_table('alert_state',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('severity', sa.String(length=10), nullable=False),
    sa.Column('issue', sa.String(length=240), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('account', sa.String(length=160), nullable=True),
    sa.Column('service', sa.String(length=80), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('audit_log',
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('actor', sa.String(length=280), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index('ix_audit_account_created', ['account_id', 'created_at'], unique=False)

    op.create_table('lead',
    sa.Column('topic', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('company', sa.String(length=160), nullable=False),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('country', sa.String(length=80), nullable=True),
    sa.Column('services', sa.JSON(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('preferred_date', sa.String(length=10), nullable=True),
    sa.Column('preferred_window', sa.String(length=20), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('stage', sa.String(length=20), server_default='new', nullable=False),
    sa.Column('stage_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('demo_status', sa.String(length=12), nullable=True),
    sa.Column('demo_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lost_reason', sa.String(length=160), nullable=True),
    sa.Column('owner_staff_id', sa.String(length=32), nullable=True),
    sa.Column('value_cents', sa.Integer(), nullable=True),
    sa.Column('account_id', sa.String(length=32), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('lead', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lead_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_lead_email'), ['email'], unique=False)
        batch_op.create_index('ix_lead_stage_created', ['stage', 'created_at'], unique=False)

    op.create_table('metric_snapshot',
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('mrr_cents', sa.Integer(), nullable=False),
    sa.Column('customers', sa.Integer(), nullable=False),
    sa.Column('subscriptions', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('day')
    )
    op.create_table('pending_signup',
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('company', sa.String(length=160), nullable=False),
    sa.Column('full_name', sa.String(length=120), nullable=True),
    sa.Column('country', sa.String(length=80), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('services', sa.JSON(), nullable=False),
    sa.Column('interval', sa.String(length=5), nullable=False),
    sa.Column('mode', sa.String(length=5), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email', name='uq_pending_signup_email'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_table('service',
    sa.Column('slug', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('tagline', sa.String(length=200), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('base_url', sa.String(length=255), nullable=False),
    sa.Column('internal_url', sa.String(length=255), nullable=True),
    sa.Column('sso_callback_path', sa.String(length=120), nullable=False),
    sa.Column('client_secret_hash', sa.String(length=64), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('is_public', sa.Boolean(), nullable=False),
    sa.Column('uses_odoo', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('setting',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_by', sa.String(length=254), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('staff_user',
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('full_name', sa.String(length=120), nullable=True),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('is_static', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_login_count', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email', name='uq_staff_email')
    )
    op.create_table('stripe_event',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('type', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('user_session',
    sa.Column('scope', sa.String(length=10), nullable=False),
    sa.Column('subject_id', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_session_subject_id'), ['subject_id'], unique=False)

    op.create_table('app_user',
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('full_name', sa.String(length=120), nullable=True),
    sa.Column('hashed_password', sa.String(length=255), nullable=True),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_login_count', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('link_token_hash', sa.String(length=64), nullable=True),
    sa.Column('link_token_purpose', sa.String(length=12), nullable=True),
    sa.Column('link_token_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('link_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email', name='uq_user_email')
    )
    with op.batch_alter_table('app_user', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_app_user_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_app_user_email'), ['email'], unique=False)
        batch_op.create_index(batch_op.f('ix_app_user_link_token_hash'), ['link_token_hash'], unique=False)

    op.create_table('lead_event',
    sa.Column('lead_id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('from_value', sa.String(length=20), nullable=True),
    sa.Column('to_value', sa.String(length=20), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('actor', sa.String(length=254), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['lead_id'], ['lead.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('lead_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lead_event_lead_id'), ['lead_id'], unique=False)

    op.create_table('odoo_connection',
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('db_name', sa.String(length=120), nullable=False),
    sa.Column('login', sa.String(length=255), nullable=False),
    sa.Column('api_key_enc', sa.Text(), nullable=False),
    sa.Column('url_key', sa.String(length=500), nullable=False),
    sa.Column('db_key', sa.String(length=120), nullable=False),
    sa.Column('db_uuid', sa.String(length=64), nullable=True),
    sa.Column('odoo_version', sa.String(length=32), nullable=True),
    sa.Column('api', sa.String(length=10), nullable=True),
    sa.Column('companies', sa.JSON(), nullable=True),
    sa.Column('key_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('key_warned_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('status_message', sa.Text(), nullable=True),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failure_notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failing_since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('db_uuid', name='uq_odoo_db_uuid'),
    sa.UniqueConstraint('url_key', 'db_key', name='uq_odoo_url_db')
    )
    with op.batch_alter_table('odoo_connection', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_odoo_connection_account_id'), ['account_id'], unique=False)

    op.create_table('service_package',
    sa.Column('service_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('description', sa.String(length=240), nullable=True),
    sa.Column('monthly_price_cents', sa.Integer(), nullable=True),
    sa.Column('yearly_price_cents', sa.Integer(), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('stripe_price_id', sa.String(length=80), nullable=True),
    sa.Column('stripe_yearly_price_id', sa.String(length=80), nullable=True),
    sa.Column('entitlements', sa.JSON(), nullable=False),
    sa.Column('features', sa.JSON(), nullable=False),
    sa.Column('trial_days', sa.Integer(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['service.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('service_id', 'name', name='uq_package_name')
    )
    with op.batch_alter_table('service_package', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_service_package_service_id'), ['service_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_service_package_stripe_price_id'), ['stripe_price_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_service_package_stripe_yearly_price_id'), ['stripe_yearly_price_id'], unique=False)

    op.create_table('account_service',
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('service_id', sa.String(length=32), nullable=False),
    sa.Column('package_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stripe_item_id', sa.String(length=64), nullable=True),
    sa.Column('entitlement_overrides', sa.JSON(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('provisioned_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('provision_error', sa.String(length=400), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('provisioned_revision', sa.Integer(), nullable=False),
    sa.Column('odoo_connection_id', sa.String(length=32), nullable=True),
    sa.Column('odoo_readiness', sa.JSON(), nullable=True),
    sa.Column('odoo_readiness_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['account.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['odoo_connection_id'], ['odoo_connection.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['package_id'], ['service_package.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['service_id'], ['service.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'service_id', name='uq_account_service')
    )
    with op.batch_alter_table('account_service', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_account_service_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_account_service_odoo_connection_id'), ['odoo_connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_account_service_package_id'), ['package_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_account_service_service_id'), ['service_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_account_service_stripe_item_id'), ['stripe_item_id'], unique=False)

    op.create_table('sso_code',
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('service_id', sa.String(length=32), nullable=False),
    sa.Column('redirect_uri', sa.String(length=500), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['service_id'], ['service.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_hash')
    )


def downgrade() -> None:
    op.drop_table('sso_code')
    with op.batch_alter_table('account_service', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_account_service_stripe_item_id'))
        batch_op.drop_index(batch_op.f('ix_account_service_service_id'))
        batch_op.drop_index(batch_op.f('ix_account_service_package_id'))
        batch_op.drop_index(batch_op.f('ix_account_service_odoo_connection_id'))
        batch_op.drop_index(batch_op.f('ix_account_service_account_id'))

    op.drop_table('account_service')
    with op.batch_alter_table('service_package', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_service_package_stripe_yearly_price_id'))
        batch_op.drop_index(batch_op.f('ix_service_package_stripe_price_id'))
        batch_op.drop_index(batch_op.f('ix_service_package_service_id'))

    op.drop_table('service_package')
    with op.batch_alter_table('odoo_connection', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_odoo_connection_account_id'))

    op.drop_table('odoo_connection')
    with op.batch_alter_table('lead_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lead_event_lead_id'))

    op.drop_table('lead_event')
    with op.batch_alter_table('app_user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_app_user_link_token_hash'))
        batch_op.drop_index(batch_op.f('ix_app_user_email'))
        batch_op.drop_index(batch_op.f('ix_app_user_account_id'))

    op.drop_table('app_user')
    with op.batch_alter_table('user_session', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_session_subject_id'))

    op.drop_table('user_session')
    op.drop_table('stripe_event')
    op.drop_table('staff_user')
    op.drop_table('setting')
    op.drop_table('service')
    op.drop_table('pending_signup')
    op.drop_table('metric_snapshot')
    with op.batch_alter_table('lead', schema=None) as batch_op:
        batch_op.drop_index('ix_lead_stage_created')
        batch_op.drop_index(batch_op.f('ix_lead_email'))
        batch_op.drop_index(batch_op.f('ix_lead_account_id'))

    op.drop_table('lead')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_account_created')

    op.drop_table('audit_log')
    op.drop_table('alert_state')
    with op.batch_alter_table('account_closure', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_account_closure_account_id'))

    op.drop_table('account_closure')
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_account_stripe_subscription_id'))
        batch_op.drop_index(batch_op.f('ix_account_stripe_customer_id'))
        batch_op.drop_index(batch_op.f('ix_account_slug'))

    op.drop_table('account')
