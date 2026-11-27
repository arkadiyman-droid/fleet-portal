"""server_samples: delay and loss to the router and to the internet

Revision ID: a4c6e8f0b2d1
Revises: e7a9c1d3f5b8
Create Date: 2026-11-26
"""
from alembic import op
import sqlalchemy as sa

revision = "a4c6e8f0b2d1"
down_revision = "e7a9c1d3f5b8"
branch_labels = None
depends_on = None

COLS = ("router_ms", "router_loss_pct", "internet_ms", "internet_loss_pct")


def upgrade():
    with op.batch_alter_table("server_samples") as b:
        for c in COLS:
            b.add_column(sa.Column(c, sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table("server_samples") as b:
        for c in reversed(COLS):
            b.drop_column(c)
