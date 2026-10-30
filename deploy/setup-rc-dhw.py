#!/usr/bin/env python3
"""
deploy/setup-rc-dhw.py — create the Whitney Young DHW towers for scraping.

Run on beverly-pi, as pi, from ~/fleet-portal (AFTER the controller exists —
see `flask rc-dhw-add whitney-young http://162.184.200.208:47002`):

    .venv/bin/python deploy/setup-rc-dhw.py [--controller whitney-young] [--dry-run]

Creates, idempotently, two RcDhwTower rows on the controller, each linked to its
existing Nepperhan building (see app/rc_dhw.py):
    Tower A  device 200000  group 200000GRP2  ->  358 Nepperhan Ave  (plant bldg A)
    Tower B  device 100000  group 100000GRP2  ->  354 Nepperhan Ave  (plant bldg B)
Both DHW graphics share one template, so the same gel-id map reads either.
Re-runnable; existing towers are updated, not duplicated. Prints a summary only.

Note: 358 = plant building A, 354 = plant building B (per the controller's own
page titles "DHW System Tower A 358" / "Tower B 354"), matching the Bloom
plant_tag mapping on those buildings.
"""
import argparse
import os
import sys
from pathlib import Path

os.environ["FLEET_POLLER_ENABLED"] = "0"
os.environ["BILLING_SCHEDULER_ENABLED"] = "0"
os.environ["LEGACY_POLLER_ENABLED"] = "0"
os.environ["BLOOM_POLLER_ENABLED"] = "0"
os.environ["RC_DHW_POLLER_ENABLED"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# tower -> (device, group, building name pattern, label)
TOWERS = [
    ("A", "200000", "200000GRP2", "358", "DHW System Tower A 358"),
    ("B", "100000", "100000GRP2", "354", "DHW System Tower B 354"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="whitney-young")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app import create_app
    from app.extensions import db
    from app.models import RcController, RcDhwTower, Building

    app = create_app()
    with app.app_context():
        ctrl = RcController.query.filter_by(label=args.controller).first()
        if not ctrl:
            print(f"ERROR: no RcController labelled {args.controller!r}. "
                  f"Run `flask rc-dhw-add {args.controller} http://162.184.200.208:47002` first.")
            sys.exit(1)

        made, updated, unlinked = 0, 0, []
        for tower, device, group, bpat, label in TOWERS:
            b = (Building.query
                 .filter(Building.name.ilike(f"{bpat}%Nepperhan%"))
                 .first())
            if b is None:
                unlinked.append(f"{bpat} Nepperhan (building not found)")

            row = RcDhwTower.query.filter_by(controller_id=ctrl.id, group=group).first()
            if row is None:
                row = RcDhwTower(controller_id=ctrl.id, group=group)
                db.session.add(row)
                made += 1
            else:
                updated += 1
            row.tower, row.device, row.label, row.is_active = tower, device, label, True
            row.building_id = b.id if b else row.building_id
            print(f"  tower {tower}: device {device} {group} -> "
                  f"{(b.name if b else 'UNLINKED')} (building_id={b.id if b else None})")

        if args.dry_run:
            db.session.rollback()
            print("DRY RUN — nothing committed.")
        else:
            db.session.commit()
            print(f"Done: {made} created, {updated} updated.")
        if unlinked:
            print("WARNING unlinked towers: " + "; ".join(unlinked))


if __name__ == "__main__":
    main()
