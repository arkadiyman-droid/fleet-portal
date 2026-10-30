#!/usr/bin/env python3
"""
deploy/import-skyline.py — put Skyline Terrace's Bloom buildings on /skyline.

Run on beverly-pi, as pi, from ~/fleet-portal (after the Bloom account exists —
see `flask bloom-add-account`):

    .venv/bin/python deploy/import-skyline.py [--account skyline] [--dry-run]

Discovers the buildings straight from Bloom: logs in, reads /api/assets, and
takes every building tagged accounts == "skyline-terrace" (Bloom's own
authoritative grouping — see app/bloom.SKYLINE_ACCOUNT). No hand-kept list, so
the set is always exactly what Bloom says it is.

Creates, idempotently (Client/Property by name, Building by name under the
property, BloomAsset by account+asset_key where asset_key is the building locid):
    Client  "Skyline Properties"
      Property "Skyline Terrace" (Canton, OH)
        Building per address  ->  BloomAsset per building
Re-runnable; existing rows are updated, not duplicated. Prints counts only.
"""
import argparse
import os
import sys
from pathlib import Path

os.environ["FLEET_POLLER_ENABLED"] = "0"
os.environ["BILLING_SCHEDULER_ENABLED"] = "0"
os.environ["LEGACY_POLLER_ENABLED"] = "0"
os.environ["BLOOM_POLLER_ENABLED"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLIENT_NAME = "Skyline Properties"
PROPERTY_NAME = "Skyline Terrace"
PROPERTY_ADDRESS = "Alan Page Dr, Canton, OH"
CITY = "Canton, OH"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="skyline", help="BloomAccount label")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import requests
    from app import bloom, create_app
    from app.extensions import db
    from app.models import BloomAccount, BloomAsset, Building, Client, Property

    app = create_app()
    with app.app_context():
        acct = BloomAccount.query.filter_by(label=args.account).first()
        if acct is None:
            sys.exit(f"No Bloom account labelled {args.account!r}. "
                     f"Run:  flask bloom-add-account {args.account}")

        # Pull the authoritative set from Bloom.
        s = requests.Session()
        tok = acct.token or bloom.login(acct, s)
        assets, _ = bloom.fetch_json(acct, s, tok, "/api/assets")
        found = [b for b in bloom.buildings(assets) if bloom.is_skyline(b)]
        found.sort(key=lambda b: b.get("name") or "")
        if not found:
            sys.exit("Bloom returned no buildings tagged 'skyline-terrace'.")
        print(f"Bloom reports {len(found)} skyline-terrace buildings.")

        client = Client.query.filter_by(name=CLIENT_NAME).first()
        if client is None:
            client = Client(name=CLIENT_NAME, billing_method="invoice")
            db.session.add(client); db.session.flush()
            print(f"client {CLIENT_NAME!r}: created (id {client.id})")
        else:
            print(f"client {CLIENT_NAME!r}: already there (id {client.id})")

        prop = Property.query.filter_by(client_id=client.id, name=PROPERTY_NAME).first()
        if prop is None:
            prop = Property(client_id=client.id, name=PROPERTY_NAME, address=PROPERTY_ADDRESS)
            db.session.add(prop); db.session.flush()
            print(f"property {PROPERTY_NAME!r}: created (id {prop.id})")
        else:
            print(f"property {PROPERTY_NAME!r}: already there (id {prop.id})")

        b_new = b_upd = a_new = a_upd = 0
        for bld in found:
            name = (bld.get("name") or "").strip()
            key = (bld.get("locid") or name).strip()
            if not name:
                continue
            address = f"{name}, {CITY}"

            building = Building.query.filter_by(property_id=prop.id, name=name).first()
            if building is None:
                building = Building(property_id=prop.id, name=name, address=address)
                db.session.add(building); db.session.flush()
                b_new += 1
            else:
                if not building.address:
                    building.address = address
                b_upd += 1

            asset = (BloomAsset.query.filter_by(account_id=acct.id, asset_key=key).first()
                     or BloomAsset.query.filter_by(account_id=acct.id, name=name).first())
            if asset is None:
                db.session.add(BloomAsset(account_id=acct.id, asset_key=key, name=name,
                                          address=address, building_id=building.id))
                a_new += 1
            else:
                asset.asset_key, asset.name, asset.address = key, name, address
                asset.building_id, asset.is_active, asset.deleted_at = building.id, True, None
                a_upd += 1

        print(f"buildings: {b_new} new, {b_upd} updated")
        print(f"bloom assets: {a_new} new, {a_upd} updated")
        if args.dry_run:
            db.session.rollback()
            print("dry run: nothing saved")
        else:
            db.session.commit()
            print(f"saved. The Bloom poller reads them within "
                  f"{app.config.get('BLOOM_POLL_INTERVAL_SECONDS', 60)}s; see /skyline.")


if __name__ == "__main__":
    main()
