"""legacy boxes: history (readings partitioned by month, hourly, daily, events)

Revision ID: b3e7c9a1d5f2
Revises: a8d4e2f6c1b3
Create Date: 2026-10-15 00:30:00

legacy_readings is a partitioned table (PARTITION BY RANGE (ts)); the monthly
partitions are created and dropped by app/history.py, so dropping a month of
old reads is instant and leaves nothing to vacuum.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'b3e7c9a1d5f2'
down_revision = 'a8d4e2f6c1b3'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE legacy_readings (
            box_id integer NOT NULL,
            ts timestamp without time zone NOT NULL,
            code varchar(12),
            "out" smallint, inside smallint, dit smallint, aqua smallint, coil smallint, mix smallint,
            supp smallint, retn smallint, risr smallint, stack smallint, hi_stack smallint,
            valve smallint, pressure smallint,
            heat1 smallint, hw1 smallint, heat2 smallint, hw2 smallint,
            heat3 smallint, hw3 smallint, heat4 smallint, hw4 smallint,
            fuel_level smallint, gallons integer, fuel_count bigint, water_count bigint, ign_count smallint,
            outputs integer, inputs integer,
            sys smallint[], apt smallint[],
            PRIMARY KEY (box_id, ts)
        ) PARTITION BY RANGE (ts)
    """)
    op.execute("CREATE INDEX ix_legacy_readings_ts ON legacy_readings USING brin (ts)")
    op.create_table(
        'legacy_hourly',
        sa.Column('box_id', sa.Integer(), sa.ForeignKey('legacy_boxes.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('hour', sa.DateTime(), primary_key=True),
        sa.Column('n', sa.SmallInteger(), nullable=False),
        sa.Column('out_avg', sa.Float()), sa.Column('out_fleet', sa.Boolean(), nullable=False),
        sa.Column('in_avg', sa.Float()), sa.Column('in_min', sa.SmallInteger()), sa.Column('in_max', sa.SmallInteger()),
        sa.Column('dit', sa.SmallInteger()),
        sa.Column('aqua_avg', sa.Float()), sa.Column('aqua_max', sa.SmallInteger()),
        sa.Column('supp_avg', sa.Float()), sa.Column('retn_avg', sa.Float()),
        sa.Column('coil_min', sa.SmallInteger()), sa.Column('coil_max', sa.SmallInteger()),
        sa.Column('mix_min', sa.SmallInteger()), sa.Column('mix_max', sa.SmallInteger()),
        sa.Column('stack_avg', sa.Float()), sa.Column('stack_max', sa.SmallInteger()), sa.Column('hi_stack', sa.SmallInteger()),
        sa.Column('heat_min', sa.SmallInteger(), nullable=False), sa.Column('hw_min', sa.SmallInteger(), nullable=False),
        sa.Column('calling_min', sa.SmallInteger(), nullable=False), sa.Column('safety_min', sa.SmallInteger(), nullable=False),
        sa.Column('bypass_min', sa.SmallInteger(), nullable=False),
        sa.Column('fuel', sa.Integer()), sa.Column('water', sa.Integer()),
        sa.Column('hdd', sa.Float(), nullable=False),
        sa.Column('law_req', sa.SmallInteger()), sa.Column('law_low', sa.SmallInteger(), nullable=False),
        sa.Column('apt_low', sa.SmallInteger()),
        sa.Column('apt_avg', postgresql.ARRAY(sa.SmallInteger())),
    )
    op.create_index('ix_legacy_hourly_hour', 'legacy_hourly', ['hour'])
    op.create_table(
        'legacy_daily',
        sa.Column('box_id', sa.Integer(), sa.ForeignKey('legacy_boxes.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('day', sa.Date(), primary_key=True),
        sa.Column('n', sa.Integer(), nullable=False), sa.Column('hours', sa.SmallInteger(), nullable=False),
        sa.Column('out_avg', sa.Float()), sa.Column('out_min', sa.Float()), sa.Column('out_max', sa.Float()),
        sa.Column('hdd', sa.Float(), nullable=False),
        sa.Column('in_avg', sa.Float()), sa.Column('in_min', sa.SmallInteger()), sa.Column('in_max', sa.SmallInteger()),
        sa.Column('dit', sa.SmallInteger()),
        sa.Column('heat_min', sa.Integer(), nullable=False), sa.Column('hw_min', sa.Integer(), nullable=False),
        sa.Column('calling_min', sa.Integer(), nullable=False), sa.Column('safety_min', sa.Integer(), nullable=False),
        sa.Column('fuel', sa.Integer()), sa.Column('water', sa.Integer()), sa.Column('water_night', sa.Integer()),
        sa.Column('law_hours', sa.SmallInteger(), nullable=False), sa.Column('law_sensor_hours', sa.Integer(), nullable=False),
        sa.Column('apt_low', sa.SmallInteger()), sa.Column('stack_fire', sa.Float()),
        sa.Column('overheat_hours', sa.SmallInteger(), nullable=False), sa.Column('warm_heat_min', sa.Integer(), nullable=False),
    )
    op.create_index('ix_legacy_daily_day', 'legacy_daily', ['day'])
    op.create_table(
        'legacy_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('box_id', sa.Integer(), sa.ForeignKey('legacy_boxes.id', ondelete='CASCADE'), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('code', sa.String(length=40)),
        sa.Column('text', sa.String(length=200)),
    )
    op.create_index('ix_legacy_events_ts', 'legacy_events', ['ts'])
    op.create_index('ix_legacy_events_box_ts', 'legacy_events', ['box_id', 'ts'])


def downgrade():
    op.drop_table('legacy_events')
    op.drop_table('legacy_daily')
    op.drop_table('legacy_hourly')
    op.execute("DROP TABLE legacy_readings")              # and every monthly partition with it
