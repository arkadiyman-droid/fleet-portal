"""bloom_fan_auto: per-property switch for automatic fan schedule switching

Revision ID: a2b4c6d8e0f1
Revises: f1d3b7a9c2e4
Create Date: 2026-11-16 20:30:00

One row per property a person has decided on: enabled means the portal's
poller may point that property's fan groups at the schedule slot for its
current plan band (app/bloom_fans.auto_switch). Off by default — no row.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a2b4c6d8e0f1'
down_revision = 'f1d3b7a9c2e4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bloom_fan_auto',
        sa.Column('prop', sa.String(length=200), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('prop'),
    )


def downgrade():
    op.drop_table('bloom_fan_auto')
