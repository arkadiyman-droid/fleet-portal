"""bloom_fan_writes: the record of every change the portal sends to Bloom

Revision ID: f1d3b7a9c2e4
Revises: e2c6a9f4b7d1
Create Date: 2026-11-16 15:30:00

The Fans page can now load the plan's schedules into a building's Bloom
schedule slots (app/bloom_fans.plan_push). Every such write is logged here:
who confirmed it, the reason, each point's old and new value, Bloom's reply.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'f1d3b7a9c2e4'
down_revision = 'e2c6a9f4b7d1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bloom_fan_writes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('changes', JSONB(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('landed', sa.Boolean(), nullable=True),
        sa.Column('detail', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(['site_id'], ['bloom_fan_sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bloom_fan_writes_site_at', 'bloom_fan_writes', ['site_id', 'at'])


def downgrade():
    op.drop_index('ix_bloom_fan_writes_site_at', table_name='bloom_fan_writes')
    op.drop_table('bloom_fan_writes')
