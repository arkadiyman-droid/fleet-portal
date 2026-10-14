"""enrollments: one-time Tailscale key for a new-site adoption

Revision ID: a7c3e9d21f04
Revises: f26374f9b6cd
Create Date: 2026-10-14 01:30:00

"""
from alembic import op
import sqlalchemy as sa
import app.crypto_types


revision = 'a7c3e9d21f04'
down_revision = 'f26374f9b6cd'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('enrollments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ts_key', app.crypto_types.EncryptedString(), nullable=True))
        batch_op.add_column(sa.Column('ts_key_made', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('ts_error', sa.String(length=300), nullable=True))


def downgrade():
    with op.batch_alter_table('enrollments', schema=None) as batch_op:
        batch_op.drop_column('ts_error')
        batch_op.drop_column('ts_key_made')
        batch_op.drop_column('ts_key')
