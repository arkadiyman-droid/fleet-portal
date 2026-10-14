#!/usr/bin/env python3
"""
deploy/add-legacy-login.py — another login for the installed gateways.

Most boxes share one login ("main", imported with import-legacy-boxes.py);
the rest refuse it. Run this on beverly-pi, as pi, from ~/fleet-portal:

    .venv/bin/python deploy/add-legacy-login.py

It asks for a name for the login, the user name and the password (hidden),
stores them encrypted, then tries the new login ONLY on boxes that refused
their current one (and are confirmed USEnergy gateways), by Digest, a few
at a time, gently. Each box that accepts it is moved to the new login.
Prints counts only, never the login or an address.
"""
import getpass
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ["FLEET_POLLER_ENABLED"] = "0"
os.environ["BILLING_SCHEDULER_ENABLED"] = "0"
os.environ["LEGACY_POLLER_ENABLED"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from requests.auth import HTTPDigestAuth

_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]|\x1b.|[\x00-\x1f\x7f]")


def ask(prompt):
    return _ESC.sub("", input(prompt)).strip()


def accepts(url, user, password):
    try:
        r = requests.get(f"{url}/obix/status", auth=HTTPDigestAuth(user, password), timeout=40)
        return r.status_code == 200
    except requests.RequestException:
        return None


def main():
    from app import create_app
    from app.extensions import db
    from app.models import LegacyBox, LegacyLogin, LegacyStatus
    app = create_app()
    with app.app_context():
        refused = (LegacyBox.query.join(LegacyStatus).filter(LegacyStatus.auth_failed.is_(True),
                                                              LegacyBox.identity.in_(("gateway", "open"))).all())
        print(f"{len(refused)} box(es) refused their current login and are confirmed gateways.")
        if not refused:
            return
        label = ask("A name for this login (e.g. second): ") or "second"
        if LegacyLogin.query.filter_by(label=label).first():
            sys.exit(f"A login named {label!r} already exists. Pick another name.")
        user = ask("User name: ")
        pw = getpass.getpass("Password (hidden): ")
        if not user or not pw:
            sys.exit("User name and password are both needed. Nothing saved.")
        print(f"Trying it on {len(refused)} box(es), a few at a time...")
        jobs = [(b.id, b.url) for b in refused]

        def one(job):
            time.sleep(1)
            return job[0], accepts(job[1], user, pw)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(one, jobs))
        ok = [bid for bid, r in results if r]
        no = sum(1 for _bid, r in results if r is False)
        gone = sum(1 for _bid, r in results if r is None)
        if not ok:
            print(f"No box accepted it ({no} refused, {gone} did not answer). Nothing saved.")
            return
        login = LegacyLogin(label=label, username=user, password=pw)
        db.session.add(login)
        db.session.flush()
        for bid in ok:
            box = db.session.get(LegacyBox, bid)
            box.login_id = login.id
            if box.status:
                box.status.auth_failed = False
                box.status.error = "login changed: read on the next poll"
        db.session.commit()
        print(f"Saved login {label!r} (encrypted). {len(ok)} box(es) moved to it; "
              f"{no} still refuse it; {gone} did not answer this time.")
        print("They are read on the next poll (within 5 minutes).")


if __name__ == "__main__":
    main()
