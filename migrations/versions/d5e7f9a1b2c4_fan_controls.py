"""fan_controls: who may run which property's fans

Revision ID: d5e7f9a1b2c4
Revises: c4d6e8f0a1b3
Create Date: 2026-11-18 16:00:00

One row per person and property: that person may open the property's
'Schedules & switching' page on the Fans page and use it. No row means the
fans there are the administrator's alone, as before.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd5e7f9a1b2c4'
down_revision = 'c4d6e8f0a1b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'fan_controls',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('property_id', sa.Integer(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'property_id', name='uq_fan_controls_user_property'),
    )


def downgrade():
    op.drop_table('fan_controls')
