"""bloom_fan_plan: the fan plan's hours as edited on the Fans page

Revision ID: c4d6e8f0a1b3
Revises: b3c5d7e9f1a2
Create Date: 2026-11-18 12:00:00

One row per save: for each temperature band, the 24 hours of the day the fans
run, as a string of 0s and 1s. The newest row is the plan in force
(app/bloom_fans.current_plan); no row means the built-in hours. Rows are never
edited, so the table is also the record of who changed the plan and when.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'c4d6e8f0a1b3'
down_revision = 'b3c5d7e9f1a2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bloom_fan_plan',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('hours', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('bloom_fan_plan')
