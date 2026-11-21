"""setting_changes: the settings ledger — who changed which setting of which building

Revision ID: a9c1e3f5b7d2
Revises: f7a9b1c3d4e6
Create Date: 2026-11-20 19:30:00

One row per setting that changed, for every kind of box the portal watches
(app/setting_audit.py): written when a person sends a change through the
portal (their name and reason) and when a poll finds a setting different
from the poll before (changed outside the portal).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'a9c1e3f5b7d2'
down_revision = 'f7a9b1c3d4e6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'setting_changes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('building_id', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(length=12), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('device', sa.String(length=200), nullable=True),
        sa.Column('key', sa.String(length=60), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('old', JSONB(), nullable=True),
        sa.Column('new', JSONB(), nullable=True),
        sa.Column('old_text', sa.String(length=120), nullable=True),
        sa.Column('new_text', sa.String(length=120), nullable=True),
        sa.Column('origin', sa.String(length=10), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('seen_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['building_id'], ['buildings.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_setting_changes_building_at', 'setting_changes', ['building_id', 'at'])
    op.create_index('ix_setting_changes_source_at', 'setting_changes', ['source', 'source_id', 'at'])
    op.create_index('ix_setting_changes_at', 'setting_changes', ['at'])


def downgrade():
    op.drop_index('ix_setting_changes_at', table_name='setting_changes')
    op.drop_index('ix_setting_changes_source_at', table_name='setting_changes')
    op.drop_index('ix_setting_changes_building_at', table_name='setting_changes')
    op.drop_table('setting_changes')
