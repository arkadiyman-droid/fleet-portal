"""software rollouts: rollouts + rollout_targets

Revision ID: d9a1c7e3b5f2
Revises: c4e8b2f9a1d7
Create Date: 2026-10-14 15:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'd9a1c7e3b5f2'
down_revision = 'c4e8b2f9a1d7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'rollouts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('target_sha', sa.String(length=40), nullable=False),
        sa.Column('state', sa.String(length=12), nullable=False, server_default='running'),
        sa.Column('wave', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('wave_done_at', sa.DateTime(), nullable=True),
        sa.Column('batch_size', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('soak_minutes', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('created_by', sa.String(length=120), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'rollout_targets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('rollout_id', sa.Integer(), nullable=False),
        sa.Column('controller_id', sa.Integer(), nullable=False),
        sa.Column('wave', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('state', sa.String(length=12), nullable=False, server_default='pending'),
        sa.Column('from_sha', sa.String(length=40), nullable=True),
        sa.Column('first_try_at', sa.DateTime(), nullable=True),
        sa.Column('requested_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('detail', sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(['controller_id'], ['controllers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['rollout_id'], ['rollouts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('rollout_id', 'controller_id', name='uq_rollout_target'),
    )
    with op.batch_alter_table('rollout_targets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_rollout_targets_rollout_id'), ['rollout_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_rollout_targets_controller_id'), ['controller_id'], unique=False)


def downgrade():
    with op.batch_alter_table('rollout_targets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_rollout_targets_controller_id'))
        batch_op.drop_index(batch_op.f('ix_rollout_targets_rollout_id'))
    op.drop_table('rollout_targets')
    op.drop_table('rollouts')
