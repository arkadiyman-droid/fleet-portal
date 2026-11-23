"""property unit counts: the subscription bills apartment units per property

Revision ID: d5f7b9c1e3a6
Revises: c3e5a7b9d1f4
Create Date: 2026-11-23 01:00:00

properties.unit_count / units_source / units_set_by / units_set_at
property_unit_changes   who changed a property's billed units, when, from what to what
(app/subscriptions.py property_units, Billing -> Units)
"""
from alembic import op
import sqlalchemy as sa


revision = 'd5f7b9c1e3a6'
down_revision = 'c3e5a7b9d1f4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('properties', sa.Column('unit_count', sa.Integer(), nullable=True))
    op.add_column('properties', sa.Column('units_source', sa.String(length=160), nullable=True))
    op.add_column('properties', sa.Column('units_set_by', sa.String(length=120), nullable=True))
    op.add_column('properties', sa.Column('units_set_at', sa.DateTime(), nullable=True))
    op.create_check_constraint('ck_properties_unit_count_positive', 'properties', 'unit_count IS NULL OR unit_count > 0')
    op.create_table(
        'property_unit_changes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('property_id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('by', sa.String(length=120), nullable=True),
        sa.Column('old_units', sa.Integer(), nullable=True),
        sa.Column('new_units', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(length=160), nullable=True),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_property_unit_changes_property_id', 'property_unit_changes', ['property_id'])


def downgrade():
    op.drop_index('ix_property_unit_changes_property_id', table_name='property_unit_changes')
    op.drop_table('property_unit_changes')
    op.drop_constraint('ck_properties_unit_count_positive', 'properties', type_='check')
    op.drop_column('properties', 'units_set_at')
    op.drop_column('properties', 'units_set_by')
    op.drop_column('properties', 'units_source')
    op.drop_column('properties', 'unit_count')
