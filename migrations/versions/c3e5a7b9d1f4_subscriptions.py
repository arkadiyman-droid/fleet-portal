"""subscriptions: owners pay monthly or quarterly by the apartment unit, card on file

Revision ID: c3e5a7b9d1f4
Revises: b1d3f5a7c9e2
Create Date: 2026-11-21 04:00:00

subscription_rates     the price per unit (newest row wins)
subscriptions          an owner's Stripe subscription as last seen
subscription_charges   each Stripe invoice of it: paid or failed
billing_controls       who may manage which owner's subscription
(app/subscriptions.py)
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3e5a7b9d1f4'
down_revision = 'b1d3f5a7c9e2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'subscription_rates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('monthly_cents', sa.Integer(), nullable=False),
        sa.Column('quarterly_cents', sa.Integer(), nullable=False),
        sa.CheckConstraint('monthly_cents > 0 AND quarterly_cents > 0', name='ck_subscription_rates_positive'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('stripe_subscription_id', sa.String(length=255), nullable=False),
        sa.Column('stripe_item_id', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('interval', sa.String(length=10), nullable=False),
        sa.Column('units', sa.Integer(), nullable=False),
        sa.Column('cents_per_unit', sa.Integer(), nullable=False),
        sa.Column('current_period_end', sa.DateTime(), nullable=True),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_subscription_id'),
    )
    op.create_index('ix_subscriptions_client_id', 'subscriptions', ['client_id'])
    op.create_table(
        'subscription_charges',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=False),
        sa.Column('stripe_invoice_id', sa.String(length=255), nullable=False),
        sa.Column('number', sa.String(length=60), nullable=True),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.Column('hosted_url', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_invoice_id'),
    )
    op.create_table(
        'billing_controls',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'client_id', name='uq_billing_controls_user_client'),
    )


def downgrade():
    op.drop_table('billing_controls')
    op.drop_table('subscription_charges')
    op.drop_index('ix_subscriptions_client_id', table_name='subscriptions')
    op.drop_table('subscriptions')
    op.drop_table('subscription_rates')
