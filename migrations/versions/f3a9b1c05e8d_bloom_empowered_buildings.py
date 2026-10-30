"""bloom / empowered buildings: bloom_accounts, bloom_assets, bloom_statuses

Revision ID: f3a9b1c05e8d
Revises: e8b4d2f0c6a3
Create Date: 2026-10-28 18:30:00

A third monitored source (see app/bloom.py): Skyline Terrace buildings read
view-only through Bloom's private JSON API. Credentials and the cached bearer
token are EncryptedString (Text at rest, Fernet-encrypted); the latest reading
is kept as raw JSONB, like legacy_statuses.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import app.crypto_types


revision = 'f3a9b1c05e8d'
down_revision = 'e8b4d2f0c6a3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bloom_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=60), nullable=False),
        sa.Column('base_url', sa.String(length=200), nullable=False,
                  server_default='https://my.empoweredbuildings.com'),
        sa.Column('username', app.crypto_types.EncryptedString(), nullable=False),
        sa.Column('password', app.crypto_types.EncryptedString(), nullable=False),
        sa.Column('token', app.crypto_types.EncryptedString(), nullable=True),
        sa.Column('token_at', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('label'),
    )
    op.create_table(
        'bloom_assets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('asset_key', sa.String(length=200), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('building_id', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('hidden_by', sa.String(length=120), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['bloom_accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['building_id'], ['buildings.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'asset_key', name='uq_bloom_asset_key'),
    )
    op.create_table(
        'bloom_statuses',
        sa.Column('asset_id', sa.Integer(), nullable=False),
        sa.Column('polled_at', sa.DateTime(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('response_ms', sa.Integer(), nullable=True),
        sa.Column('last_ok_at', sa.DateTime(), nullable=True),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['bloom_assets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('asset_id'),
    )


def downgrade():
    op.drop_table('bloom_statuses')
    op.drop_table('bloom_assets')
    op.drop_table('bloom_accounts')
