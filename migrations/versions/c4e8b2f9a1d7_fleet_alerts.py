"""fleet alerts: alert_states + alert_events

Revision ID: c4e8b2f9a1d7
Revises: a7c3e9d21f04
Create Date: 2026-10-14 11:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'c4e8b2f9a1d7'
down_revision = 'a7c3e9d21f04'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'alert_states',
        sa.Column('controller_id', sa.Integer(), nullable=False),
        sa.Column('alerting', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('first_bad_at', sa.DateTime(), nullable=True),
        sa.Column('opened_at', sa.DateTime(), nullable=True),
        sa.Column('notified_at', sa.DateTime(), nullable=True),
        sa.Column('issue', sa.String(length=300), nullable=True),
        sa.Column('muted_until', sa.DateTime(), nullable=True),
        sa.Column('muted_by', sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(['controller_id'], ['controllers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('controller_id'),
    )
    op.create_table(
        'alert_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('controller_id', sa.Integer(), nullable=True),
        sa.Column('controller_name', sa.String(length=200), nullable=True),
        sa.Column('kind', sa.String(length=12), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('text', sa.String(length=1000), nullable=True),
        sa.Column('delivered', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['controller_id'], ['controllers.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alert_events_controller_id'), ['controller_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_events_created_at'), ['created_at'], unique=False)


def downgrade():
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alert_events_created_at'))
        batch_op.drop_index(batch_op.f('ix_alert_events_controller_id'))
    op.drop_table('alert_events')
    op.drop_table('alert_states')
