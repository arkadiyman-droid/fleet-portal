"""legacy boxes: hide / delete (deleted_at, hidden_by)

Revision ID: f7c3d9e1a2b4
Revises: e5f1a8c2d4b9
Create Date: 2026-10-14 18:20:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'f7c3d9e1a2b4'
down_revision = 'e5f1a8c2d4b9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('legacy_boxes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('hidden_by', sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table('legacy_boxes', schema=None) as batch_op:
        batch_op.drop_column('hidden_by')
        batch_op.drop_column('deleted_at')
