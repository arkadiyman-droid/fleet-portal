"""bloom_fans: roof exhaust fans on Bloom buildings (the /fans page)

Revision ID: e2c6a9f4b7d1
Revises: b9e2d7c3f5a1
Create Date: 2026-11-16 12:30:00

bloom_fan_sites holds the fan-related slice of every Bloom building that has
exhaust fans (discovered by the poller, independent of bloom_assets);
bloom_fan_samples is its throttled history. See app/bloom_fans.py.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'e2c6a9f4b7d1'
down_revision = 'b9e2d7c3f5a1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bloom_fan_sites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('asset_key', sa.String(length=200), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('data', JSONB(), nullable=False),
        sa.Column('polled_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['bloom_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'asset_key', name='uq_bloom_fan_site'),
    )
    op.create_table(
        'bloom_fan_samples',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('data', JSONB(), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['bloom_fan_sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bloom_fan_samples_site_ts', 'bloom_fan_samples', ['site_id', 'ts'])


def downgrade():
    op.drop_index('ix_bloom_fan_samples_site_ts', table_name='bloom_fan_samples')
    op.drop_table('bloom_fan_samples')
    op.drop_table('bloom_fan_sites')
