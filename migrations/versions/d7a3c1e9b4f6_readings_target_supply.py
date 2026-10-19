"""legacy readings: the hot-water target (desiredSupplyTemp) with every read

Revision ID: d7a3c1e9b4f6
Revises: c5d8e2a4f7b1
Create Date: 2026-10-18 20:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'd7a3c1e9b4f6'
down_revision = 'c5d8e2a4f7b1'
branch_labels = None
depends_on = None


def upgrade():
    # a partitioned table: the column reaches every monthly partition
    op.add_column('legacy_readings', sa.Column('dsup', sa.SmallInteger(), nullable=True))


def downgrade():
    op.drop_column('legacy_readings', 'dsup')
