"""legacy hourly: hot-water target vs supply while calling (dsup_avg, supp_call_avg, short_min)

Revision ID: e8b4d2f0c6a3
Revises: d7a3c1e9b4f6
Create Date: 2026-10-19 00:30:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'e8b4d2f0c6a3'
down_revision = 'd7a3c1e9b4f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('legacy_hourly', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dsup_avg', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('supp_call_avg', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('short_min', sa.SmallInteger(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('legacy_hourly', schema=None) as batch_op:
        batch_op.drop_column('short_min')
        batch_op.drop_column('supp_call_avg')
        batch_op.drop_column('dsup_avg')
