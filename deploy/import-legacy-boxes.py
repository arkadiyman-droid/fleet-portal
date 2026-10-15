#!/usr/bin/env python3
"""
deploy/import-legacy-boxes.py — put the installed 2010 gateways on /live.

Run on beverly-pi, as pi, from ~/fleet-portal:

    .venv/bin/python deploy/import-legacy-boxes.py [--dry-run]

Reads:
  ~/fleet-portal-data/BldgList.xml   the old Windows client's building list
                                     (name + host:port per box, no logins)
  ~/fleet-portal-data/sweep.json     the no-login check of every address
                                     (which ones answered as gateways)
  ~/fleet-portal/.obix_test.json     the login proven on a real box with
                                     deploy/obix-probe.py: imported as the
                                     shared login "main" (most boxes use it)

Creates or updates one LegacyBox per building (matched by host:port, so it
can be run again), all on the "main" login; a box whose address answered as
something else ('other') is kept but never sent a login. Prints counts only:
no address, user name or password.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ["FLEET_POLLER_ENABLED"] = "0"
os.environ["BILLING_SCHEDULER_ENABLED"] = "0"
os.environ["LEGACY_POLLER_ENABLED"] = "0"
APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
DATA = Path(os.path.expanduser("~/fleet-portal-data"))

KIND = {"gateway (asks for login)": "gateway", "oBIX answers without login": "open",
        "asks for login, but NOT a USEnergy gateway": "other"}


def main():
    dry = "--dry-run" in sys.argv
    root = ET.fromstring((DATA / "BldgList.xml").read_bytes())
    rows = [{e.attrib.get("name"): e.attrib.get("val", "") for e in o} for o in root]
    sweep = {r["host"]: r["kind"] for r in json.loads((DATA / "sweep.json").read_text())}
    login = json.loads((APP / ".obix_test.json").read_text())

    from app import create_app
    from app.extensions import db
    from app.models import LegacyBox, LegacyLogin
    app = create_app()
    with app.app_context():
        main_login = LegacyLogin.query.filter_by(label="main").first()
        if main_login is None:
            main_login = LegacyLogin(label="main", username=login["user"], password=login["password"])
            db.session.add(main_login)
            db.session.flush()
            print("login 'main': stored (encrypted), from the login proven with obix-probe")
        else:
            print("login 'main': already there, left as it is")
        made = updated = skipped = 0
        kinds = {}
        seen = set()
        for r in rows:
            hostport = (r.get("hostname") or "").strip()
            if ":" not in hostport:
                skipped += 1
                continue
            host, port = hostport.rsplit(":", 1)
            if not port.isdigit() or (host, int(port)) in seen:
                skipped += 1
                continue
            seen.add((host, int(port)))
            kind = KIND.get(sweep.get(hostport, ""), "unreachable" if hostport in sweep else "unknown")
            kinds[kind] = kinds.get(kind, 0) + 1
            box = LegacyBox.query.filter_by(host=host, port=int(port)).first()
            if box is None:
                db.session.add(LegacyBox(name=r.get("buildingName") or hostport, host=host, port=int(port),
                                         identity=kind, login_id=main_login.id))
                made += 1
            else:
                # The name is only set when a box is first added: names were
                # matched to Anagha's afterwards (2026-10-15) and a re-run must
                # not put the old client's spellings back.
                if box.identity in ("unknown", "unreachable"):
                    box.identity = kind
                updated += 1
        print(f"boxes: {made} new, {updated} updated, {skipped} skipped (no or repeated address)")
        print("by what answered at the address:", ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())))
        if dry:
            db.session.rollback()
            print("dry run: nothing saved")
        else:
            db.session.commit()
            print("saved. The poller reads them within 5 minutes; see /live.")


if __name__ == "__main__":
    main()
