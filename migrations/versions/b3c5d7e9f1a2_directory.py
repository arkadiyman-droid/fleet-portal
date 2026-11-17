"""directory: the manager directory read from the violations portal

Revision ID: b3c5d7e9f1a2
Revises: a2b4c6d8e0f1
Create Date: 2026-11-17 03:00:00

directory_properties is a copy of the violations portal's property list
(managers by email, buildings, two headline counts), each row optionally
linked by the administrator to one of this portal's properties.
directory_state is the single row saying when it was last read.
permissions.source marks a grant the Directory page made ('directory'), so
only those are ever offered for removal when the directory changes.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'b3c5d7e9f1a2'
down_revision = 'a2b4c6d8e0f1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'directory_properties',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ext_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('region', sa.String(length=100), nullable=True),
        sa.Column('unit_count', sa.Integer(), nullable=True),
        sa.Column('pm_name', sa.String(length=200), nullable=True),
        sa.Column('pm_email', sa.String(length=255), nullable=True),
        sa.Column('rpm_name', sa.String(length=200), nullable=True),
        sa.Column('rpm_email', sa.String(length=255), nullable=True),
        sa.Column('site_email', sa.String(length=255), nullable=True),
        sa.Column('buildings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('open_violations', sa.Integer(), nullable=True),
        sa.Column('overdue_obligations', sa.Integer(), nullable=True),
        sa.Column('property_id', sa.Integer(), nullable=True),
        sa.Column('linked_by', sa.String(length=120), nullable=True),
        sa.Column('linked_at', sa.DateTime(), nullable=True),
        sa.Column('present', sa.Boolean(), nullable=False),
        sa.Column('seen_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ext_id', name='uq_directory_property_ext'),
    )
    op.create_index('ix_directory_properties_property_id', 'directory_properties', ['property_id'])
    op.create_table(
        'directory_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('attempted_at', sa.DateTime(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('org', sa.String(length=200), nullable=True),
        sa.Column('n', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('permissions', sa.Column('source', sa.String(length=20), nullable=True))


def downgrade():
    op.drop_column('permissions', 'source')
    op.drop_table('directory_state')
    op.drop_index('ix_directory_properties_property_id', table_name='directory_properties')
    op.drop_table('directory_properties')
