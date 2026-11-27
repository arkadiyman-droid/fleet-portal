"""server health: the Server page's per-minute samples and each server's newest snapshot

Revision ID: e7a9c1d3f5b8
Revises: d5f7b9c1e3a6
Create Date: 2026-11-25 11:00:00

server_samples   one row a minute per server: CPU, memory, disk, temperature,
                 network, disk writes, tunnel requests (app/server_health.py)
server_hosts     every server reporting, with its newest whole snapshot
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'e7a9c1d3f5b8'
down_revision = 'd5f7b9c1e3a6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'server_samples',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('host', sa.String(length=80), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        *[sa.Column(c, sa.Float(), nullable=True) for c in (
            'cpu_pct', 'load1', 'mem_used_pct', 'mem_used_mb', 'swap_used_mb', 'disk_used_pct', 'temp_c',
            'net_rx_kbps', 'net_tx_kbps', 'disk_write_kbps', 'req_per_min')],
        sa.Column('throttled', sa.Integer(), nullable=True),
        sa.Column('problems', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('host', 'at', name='uq_server_samples_host_at'),
    )
    op.create_table(
        'server_hosts',
        sa.Column('host', sa.String(length=80), nullable=False),
        sa.Column('role', sa.String(length=40), nullable=True),
        sa.Column('seen_at', sa.DateTime(), nullable=True),
        sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint('host'),
    )


def downgrade():
    op.drop_table('server_hosts')
    op.drop_table('server_samples')
