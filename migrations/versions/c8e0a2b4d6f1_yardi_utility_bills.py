"""Utilities: accounts and bills from the Yardi payables export

Revision ID: c8e0a2b4d6f1
Revises: b7d9f1a3c5e2
Create Date: 2026-11-27
"""
from alembic import op
import sqlalchemy as sa

revision = "c8e0a2b4d6f1"
down_revision = "b7d9f1a3c5e2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("utility_accounts") as t:
        t.add_column(sa.Column("ext_key", sa.String(300)))
        t.add_column(sa.Column("ext_property", sa.String(40)))
        t.add_column(sa.Column("property_id", sa.Integer(),
                               sa.ForeignKey("properties.id", ondelete="SET NULL", name="fk_utility_accounts_property")))
        t.add_column(sa.Column("kind", sa.String(12), nullable=False, server_default="building"))
        t.add_column(sa.Column("supplier", sa.Boolean(), nullable=False, server_default=sa.false()))
        t.add_column(sa.Column("links_set_by", sa.String(120)))
        t.add_column(sa.Column("links_set_at", sa.DateTime()))
    op.create_index("ix_utility_accounts_property", "utility_accounts", ["property_id"])
    op.create_index("ix_utility_accounts_source_key", "utility_accounts", ["source", "ext_key"])
    with op.batch_alter_table("utility_bills") as t:
        t.add_column(sa.Column("lines", sa.Integer()))
        t.add_column(sa.Column("invoices", sa.Integer()))
        t.add_column(sa.Column("flags", sa.String(40)))


def downgrade():
    with op.batch_alter_table("utility_bills") as t:
        for c in ("flags", "invoices", "lines"):
            t.drop_column(c)
    op.drop_index("ix_utility_accounts_source_key", table_name="utility_accounts")
    op.drop_index("ix_utility_accounts_property", table_name="utility_accounts")
    with op.batch_alter_table("utility_accounts") as t:
        for c in ("links_set_at", "links_set_by", "supplier", "kind", "property_id", "ext_property", "ext_key"):
            t.drop_column(c)
