"""rc_dhw: Reliable Controls MACH-ProWeb DHW scrape (controllers, towers, status)

Revision ID: d4a7c1e9f2b6
Revises: c71e2f4a9d80
Create Date: 2026-10-30 09:30:00

A fourth monitored source (see app/rc_dhw.py): the Whitney Young / Nepperhan
DHW System A/B, read view-only by scraping the Reliable Controls MACH-ProWeb
web UI with a headless Chromium. One controller owns several DHW towers (one
MACH-ProWeb graphic page each); each tower keeps its latest reading, with the
full gel-id map in JSONB so a graphic change needs no migration.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'd4a7c1e9f2b6'
down_revision = 'c71e2f4a9d80'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'rc_controllers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=60), nullable=False),
        sa.Column('base_url', sa.String(length=200), nullable=False),
        sa.Column('username', sa.Text(), nullable=False),
        sa.Column('password', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('label', name='uq_rc_controllers_label'),
    )
    op.create_table(
        'rc_dhw_towers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('controller_id', sa.Integer(), nullable=False),
        sa.Column('building_id', sa.Integer(), nullable=True),
        sa.Column('tower', sa.String(length=8), nullable=False),
        sa.Column('device', sa.String(length=16), nullable=False),
        sa.Column('group', sa.String(length=32), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['controller_id'], ['rc_controllers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['building_id'], ['buildings.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('controller_id', 'group', name='uq_rc_dhw_tower_group'),
    )
    op.create_table(
        'rc_dhw_statuses',
        sa.Column('tower_id', sa.Integer(), nullable=False),
        sa.Column('polled_at', sa.DateTime(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('response_ms', sa.Integer(), nullable=True),
        sa.Column('last_ok_at', sa.DateTime(), nullable=True),
        sa.Column('supply_f', sa.Float(), nullable=True),
        sa.Column('return_f', sa.Float(), nullable=True),
        sa.Column('pump_running', sa.Boolean(), nullable=True),
        sa.Column('alarm', sa.Boolean(), nullable=True),
        sa.Column('data', JSONB(), nullable=True),
        sa.ForeignKeyConstraint(['tower_id'], ['rc_dhw_towers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tower_id'),
    )


def downgrade():
    op.drop_table('rc_dhw_statuses')
    op.drop_table('rc_dhw_towers')
    op.drop_table('rc_controllers')
