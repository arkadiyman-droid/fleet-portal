"""Utilities: LL84 benchmarking, utility accounts and bills

Revision ID: b7d9f1a3c5e2
Revises: a4c6e8f0b2d1
Create Date: 2026-11-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b7d9f1a3c5e2"
down_revision = "a4c6e8f0b2d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "benchmark_properties",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(300)), sa.Column("address", sa.String(300)),
        sa.Column("borough", sa.String(20)), sa.Column("postal_code", sa.String(10)),
        sa.Column("bbl", sa.String(20)), sa.Column("bin", sa.String(200)),
        sa.Column("property_type", sa.String(120)), sa.Column("gfa", sa.Float()),
        sa.Column("units", sa.Integer()), sa.Column("n_buildings", sa.Integer()),
        sa.Column("ll97_pathway", sa.String(10)),
        sa.Column("refreshed_at", sa.DateTime()))
    op.create_table(
        "benchmark_years",
        sa.Column("property_id", sa.String(20), sa.ForeignKey("benchmark_properties.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("site_eui", sa.Float()), sa.Column("wn_site_eui", sa.Float()), sa.Column("source_eui", sa.Float()),
        sa.Column("energy_star", sa.Integer()),
        sa.Column("gas_kbtu", sa.Float()), sa.Column("oil2_kbtu", sa.Float()), sa.Column("oil4_kbtu", sa.Float()),
        sa.Column("oil56_kbtu", sa.Float()), sa.Column("steam_kbtu", sa.Float()), sa.Column("elec_kwh", sa.Float()),
        sa.Column("water_kgal", sa.Float()), sa.Column("ghg_t", sa.Float()),
        sa.Column("estimated_energy", sa.Boolean()), sa.Column("estimated_water", sa.Boolean()))
    op.create_table(
        "benchmark_stats",
        sa.Column("property_type", sa.String(120), primary_key=True),
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("n", sa.Integer()),
        sa.Column("site_eui", sa.Float()), sa.Column("wn_site_eui", sa.Float()),
        sa.Column("ghg_per_sf", sa.Float()), sa.Column("water_per_unit", sa.Float()))
    op.create_table(
        "building_benchmarks",
        sa.Column("building_id", sa.Integer(), sa.ForeignKey("buildings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("property_id", sa.String(20), sa.ForeignKey("benchmark_properties.id", ondelete="CASCADE")),
        sa.Column("how", sa.String(10), nullable=False),
        sa.Column("alternates", postgresql.JSONB()),
        sa.Column("set_by", sa.String(120)), sa.Column("set_at", sa.DateTime()))
    op.create_table(
        "utility_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("utility", sa.String(80), nullable=False),
        sa.Column("commodity", sa.String(12), nullable=False),
        sa.Column("account_number", sa.String(60)), sa.Column("meter_number", sa.String(60)),
        sa.Column("service_address", sa.String(300)), sa.Column("customer_of_record", sa.String(200)),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("notes", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
    op.create_table(
        "utility_account_buildings",
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("utility_accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("building_id", sa.Integer(), sa.ForeignKey("buildings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("share", sa.Float(), nullable=False, server_default="1.0"))
    op.create_table(
        "utility_bills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("utility_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False), sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("usage", sa.Float()), sa.Column("unit", sa.String(12)),
        sa.Column("cost_cents", sa.Integer()), sa.Column("demand_kw", sa.Float()),
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(300)),
        sa.Column("imported_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("account_id", "period_start", "period_end", name="uq_utility_bill_period"))
    op.create_index("ix_utility_bills_account", "utility_bills", ["account_id", "period_start"])


def downgrade():
    op.drop_index("ix_utility_bills_account", table_name="utility_bills")
    for t in ("utility_bills", "utility_account_buildings", "utility_accounts", "building_benchmarks",
              "benchmark_stats", "benchmark_years", "benchmark_properties"):
        op.drop_table(t)
