"""legacy boxes: alerts (legacy_alert_states, alert_events.legacy_box_id)

Revision ID: a8d4e2f6c1b3
Revises: f7c3d9e1a2b4
Create Date: 2026-10-14 23:30:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'a8d4e2f6c1b3'
down_revision = 'f7c3d9e1a2b4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'legacy_alert_states',
        sa.Column('box_id', sa.Integer(), nullable=False),
        sa.Column('alerting', sa.Boolean(), nullable=False),
        sa.Column('first_bad_at', sa.DateTime(), nullable=True),
        sa.Column('opened_at', sa.DateTime(), nullable=True),
        sa.Column('notified_at', sa.DateTime(), nullable=True),
        sa.Column('issue', sa.String(length=300), nullable=True),
        sa.Column('muted_until', sa.DateTime(), nullable=True),
        sa.Column('muted_by', sa.String(length=120), nullable=True),
        sa.Column('last_alarm', sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(['box_id'], ['legacy_boxes.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('box_id'),
    )
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('legacy_box_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_alert_events_legacy_box_id'), ['legacy_box_id'], unique=False)
        batch_op.create_foreign_key('fk_alert_events_legacy_box_id', 'legacy_boxes', ['legacy_box_id'], ['id'],
                                    ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.drop_constraint('fk_alert_events_legacy_box_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_alert_events_legacy_box_id'))
        batch_op.drop_column('legacy_box_id')
    op.drop_table('legacy_alert_states')
