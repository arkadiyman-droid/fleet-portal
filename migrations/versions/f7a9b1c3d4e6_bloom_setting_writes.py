"""bloom_setting_writes: the record of every settings change the portal sends to Bloom

Revision ID: f7a9b1c3d4e6
Revises: e6f8a0b2c3d5
Create Date: 2026-11-20 11:30:00

A Bloom heating building's settings (day and night setpoints, outdoor cutoff,
day period, heating season, secondary call for heat) can now be changed from
its page in the portal (app/bloom_settings.plan, routes.skyline_settings).
Every such write is logged here: who confirmed it, the reason, each point's
old and new value, Bloom's reply, and whether a read-back found it in place.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'f7a9b1c3d4e6'
down_revision = 'e6f8a0b2c3d5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bloom_setting_writes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('changes', JSONB(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('landed', sa.Boolean(), nullable=True),
        sa.Column('detail', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['bloom_assets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bloom_setting_writes_asset_at', 'bloom_setting_writes', ['asset_id', 'at'])


def downgrade():
    op.drop_index('ix_bloom_setting_writes_asset_at', table_name='bloom_setting_writes')
    op.drop_table('bloom_setting_writes')
