"""bloom_fan_plan.prop: a property can have fan hours of its own

Revision ID: e6f8a0b2c3d5
Revises: d5e7f9a1b2c4
Create Date: 2026-11-18 20:00:00

NULL (every existing row) is the general plan. A row carrying a property's
name holds that property's own hours; with empty hours it records the property
going back to the general plan. The newest row per property decides.
"""
from alembic import op
import sqlalchemy as sa


revision = 'e6f8a0b2c3d5'
down_revision = 'd5e7f9a1b2c4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('bloom_fan_plan', sa.Column('prop', sa.String(length=200), nullable=True))
    op.create_index('ix_bloom_fan_plan_prop', 'bloom_fan_plan', ['prop'])


def downgrade():
    op.execute("DELETE FROM bloom_fan_plan WHERE prop IS NOT NULL")
    op.drop_index('ix_bloom_fan_plan_prop', table_name='bloom_fan_plan')
    op.drop_column('bloom_fan_plan', 'prop')
