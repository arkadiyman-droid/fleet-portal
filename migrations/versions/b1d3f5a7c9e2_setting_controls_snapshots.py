"""setting_controls + setting_snapshots: who may change settings; what each box held last

Revision ID: b1d3f5a7c9e2
Revises: a9c1e3f5b7d2
Create Date: 2026-11-20 21:30:00

setting_controls: a person who may change the heating settings of one
property's buildings from the portal under their own name
(app/permissions.can_change_settings) — ticked per person and per property.

setting_snapshots: the settings of one box as last read, for the kinds of box
whose poller does not already keep the previous reading (the 2010 oBIX boxes
first): what the next read is compared with (app/setting_audit.track).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'b1d3f5a7c9e2'
down_revision = 'a9c1e3f5b7d2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'setting_controls',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('property_id', sa.Integer(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'property_id', name='uq_setting_controls_user_property'),
    )
    op.create_table(
        'setting_snapshots',
        sa.Column('source', sa.String(length=12), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('data', JSONB(), nullable=False),
        sa.PrimaryKeyConstraint('source', 'source_id'),
    )


def downgrade():
    op.drop_table('setting_snapshots')
    op.drop_table('setting_controls')
