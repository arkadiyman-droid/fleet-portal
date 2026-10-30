"""bloom_samples: time-series history for Bloom assets (the per-sensor chart)

Revision ID: b9e2d7c3f5a1
Revises: d4a7c1e9f2b6
Create Date: 2026-10-30 11:15:00

Bloom has no history of its own, so the portal records its own snapshots:
one BloomSample per asset per poll (throttled to BLOOM_SAMPLE_MIN_SECONDS),
pruned to BLOOM_RAW_DAYS. `data` is a flat {chart-key: value} map. Powers the
"click a sensor" chart on Bloom buildings (bloom.series).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'b9e2d7c3f5a1'
down_revision = 'd4a7c1e9f2b6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bloom_samples',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('data', JSONB(), nullable=False),
        sa.ForeignKeyConstraint(['asset_id'], ['bloom_assets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bloom_samples_asset_ts', 'bloom_samples', ['asset_id', 'ts'])


def downgrade():
    op.drop_index('ix_bloom_samples_asset_ts', table_name='bloom_samples')
    op.drop_table('bloom_samples')
