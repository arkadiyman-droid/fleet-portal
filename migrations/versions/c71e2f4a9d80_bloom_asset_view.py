"""bloom_assets.view + plant_tag: scope one shared feed to several sub-buildings

Revision ID: c71e2f4a9d80
Revises: f3a9b1c05e8d
Create Date: 2026-10-30 06:10:00

A single Bloom feed can cover more than one building (e.g. a two-tower plant
on one controller — Whitney Young / 354+358 Nepperhan). Allow several
BloomAsset rows to share one asset_key, each with a distinct `view` that
scopes the derived card to its own sub-building: `view` is the apartment/space
name prefix to keep (e.g. "354"/"358"), `plant_tag` the plant's building
letter (e.g. "A"/"B") used to pick "PDT BLDG <tag> SUPPLY/RETURN". The old
(account_id, asset_key) uniqueness becomes (account_id, asset_key, view).
A normal whole-feed asset uses view="" (the default), so nothing else changes.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c71e2f4a9d80'
down_revision = 'f3a9b1c05e8d'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('bloom_assets',
                  sa.Column('view', sa.String(length=20), nullable=False, server_default=''))
    op.add_column('bloom_assets',
                  sa.Column('plant_tag', sa.String(length=8), nullable=True))
    op.drop_constraint('uq_bloom_asset_key', 'bloom_assets', type_='unique')
    op.create_unique_constraint('uq_bloom_asset_view', 'bloom_assets',
                                ['account_id', 'asset_key', 'view'])


def downgrade():
    op.drop_constraint('uq_bloom_asset_view', 'bloom_assets', type_='unique')
    op.create_unique_constraint('uq_bloom_asset_key', 'bloom_assets',
                                ['account_id', 'asset_key'])
    op.drop_column('bloom_assets', 'plant_tag')
    op.drop_column('bloom_assets', 'view')
