"""controller readings: a 5-minute snapshot history of each controller, for charts

Revision ID: c5d8e2a4f7b1
Revises: b3e7c9a1d5f2
Create Date: 2026-10-15 14:00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'c5d8e2a4f7b1'
down_revision = 'b3e7c9a1d5f2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'controller_readings',
        sa.Column('controller_id', sa.Integer(), sa.ForeignKey('controllers.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('ts', sa.DateTime(), primary_key=True),
        sa.Column('values', postgresql.JSONB(), nullable=False),
        sa.Column('apt', postgresql.ARRAY(sa.SmallInteger())),
    )


def downgrade():
    op.drop_table('controller_readings')
