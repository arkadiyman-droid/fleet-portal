"""legacy oBIX boxes: legacy_logins, legacy_boxes, legacy_statuses

Revision ID: e5f1a8c2d4b9
Revises: d9a1c7e3b5f2
Create Date: 2026-10-14 17:30:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import app.crypto_types


revision = 'e5f1a8c2d4b9'
down_revision = 'd9a1c7e3b5f2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'legacy_logins',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=60), nullable=False),
        sa.Column('username', app.crypto_types.EncryptedString(), nullable=False),
        sa.Column('password', app.crypto_types.EncryptedString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('label'),
    )
    op.create_table(
        'legacy_boxes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('host', sa.String(length=200), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('identity', sa.String(length=12), nullable=False, server_default='unknown'),
        sa.Column('login_id', sa.Integer(), nullable=True),
        sa.Column('building_id', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['building_id'], ['buildings.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['login_id'], ['legacy_logins.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('host', 'port', name='uq_legacy_box_address'),
    )
    op.create_table(
        'legacy_statuses',
        sa.Column('box_id', sa.Integer(), nullable=False),
        sa.Column('polled_at', sa.DateTime(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('auth_failed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('response_ms', sa.Integer(), nullable=True),
        sa.Column('last_ok_at', sa.DateTime(), nullable=True),
        sa.Column('names_at', sa.DateTime(), nullable=True),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['box_id'], ['legacy_boxes.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('box_id'),
    )


def downgrade():
    op.drop_table('legacy_statuses')
    op.drop_table('legacy_boxes')
    op.drop_table('legacy_logins')
