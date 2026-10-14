#!/usr/bin/env python3
"""
deploy/obix-probe.py — can the portal read one of the installed oBIX boxes?

The first step toward showing the 2010-generation gateways (oBIX server
2.6.3) on the Fleet Portal. Run it on beverly-pi, as pi, from the portal's
virtualenv:

    .venv/bin/python deploy/obix-probe.py --setup   # enter one box's address + login, then test it
    .venv/bin/python deploy/obix-probe.py           # test it again with what was saved

--setup asks for the address, port, user name and password (the password is
not echoed) and keeps them in ~/fleet-portal/.obix_test.json, readable only
by pi. Nothing here ever prints the password.

It reads, and only reads: obix/about, obix/status, obix/systemnames,
obix/alarm. It reports whether the box answered from here, how fast, its
software version, and the readings an Anagha-style card would show, so the
mapping can be checked against what Anagha shows for the same building.
"""
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth

SAVED = Path(__file__).resolve().parent.parent / ".obix_test.json"
TIMEOUT = 15
_PAIR = re.compile(r'<(\w+)\s+name\s*=\s*"([^"]*)"\s*(?:val\s*=\s*"([^"]*)")?[^>]*?(?:displayName\s*=\s*"([^"]*)")?')


def setup():
    print("One installed box, to test from beverly-pi. Nothing typed here is shown back,")
    print("and the password is not echoed.")
    host = input("Address (IP or name, e.g. 96.81.94.225): ").strip()
    host = re.sub(r"^https?://", "", host).rstrip("/")
    port = input("Port (Enter for 80): ").strip() or "80"
    if ":" in host:
        host, port = host.rsplit(":", 1)
    if not re.fullmatch(r"[A-Za-z0-9.\-]+", host) or not port.isdigit():
        sys.exit("That address or port does not look right. Nothing saved.")
    user = input("User name: ").strip()
    pw = getpass.getpass("Password (hidden): ")
    if not user or not pw:
        sys.exit("User name and password are both needed. Nothing saved.")
    SAVED.write_text(json.dumps({"url": f"http://{host}:{port}", "user": user, "password": pw}))
    os.chmod(SAVED, 0o600)
    print(f"Saved (only pi can read it). Testing {host}:{port} now.\n")


def fetch(box, path, auth_kind="digest"):
    auth = HTTPDigestAuth(box["user"], box["password"])
    t = time.monotonic()
    r = requests.get(f"{box['url']}/{path}", auth=auth, timeout=TIMEOUT)
    return r, int((time.monotonic() - t) * 1000)


def values(xml):
    out = {}
    for tag, name, val, disp in _PAIR.findall(xml or ""):
        if tag in ("obj", "list", "op", "ref"):
            continue
        out[name.strip()] = (val, disp)
    return out


def main():
    if "--setup" in sys.argv or not SAVED.exists():
        setup()
    box = json.loads(SAVED.read_text())
    print(f"Box: {box['url']}  (user {box['user']})")

    # Ask WITHOUT a login first: the 2010 gateway answers 401 with a Digest
    # challenge for realm "USEnergy" (OBIX_SVR.C). Only then is a login sent,
    # and only by Digest, so the password never crosses the network readable,
    # and never goes to an address that is not one of these gateways.
    try:
        first = requests.get(f"{box['url']}/obix/about", timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        sys.exit(f"NOT REACHABLE from beverly-pi: {type(e).__name__}. The box may only be reachable "
                 "from Anagha's server, or the address/port is wrong.")
    challenge = first.headers.get("WWW-Authenticate", "")
    if first.status_code == 401 and "USEnergy" not in challenge:
        sys.exit("Something answered at that address, but it is NOT a USEnergy gateway. No login was sent.")
    if first.status_code == 401 and not challenge.lower().startswith("digest"):
        sys.exit("The gateway asked for a non-Digest login; this tool sends logins only by Digest. Nothing sent.")
    auth_kind = "digest"
    r, ms = fetch(box, "obix/about", auth_kind)
    print(f"obix/about: HTTP {r.status_code} in {ms} ms ({auth_kind} login)")
    if r.status_code == 401:
        sys.exit("The box refused the user name or password. Run with --setup to enter them again.")
    about = values(r.text)
    for k in ("productName", "productVersion", "serialNumber", "vendorName", "modelName"):
        if k in about:
            print(f"  {k}: {about[k][0]}")

    names = {}
    r, ms = fetch(box, "obix/systemnames", auth_kind)
    if r.status_code == 200:
        for k, (v, _d) in values(r.text).items():
            names[k] = v
    r, ms = fetch(box, "obix/status", auth_kind)
    print(f"\nobix/status: HTTP {r.status_code} in {ms} ms")
    st = values(r.text)
    if not st:
        print(r.text[:400])
        return
    card = ["serverTime", "callForHeat", "averageTemperature", "targetInsideTemperature",
            "stackTemperature", "highStackTemp", "highMixTemp", "lowMixTemp", "highCoilTemp", "lowCoilTemp",
            "boiler1HeatRunMinutes", "boiler1HWtrRunMinutes", "boiler2HeatRunMinutes", "boiler2HWtrRunMinutes",
            "desiredSupplyTemp", "aquastatTargetTemp", "controlValvePctOpen", "heatControlType",
            "fuelLevel", "gallons", "steamPressure", "systemVacuum", "ignitionFailCount",
            "waterMeterCount", "inputImage", "outputImage", "phaseTime"]
    for k in card:
        if k in st:
            v, d = st[k]
            print(f"  {k:24} {v}{('   (' + d + ')') if d else ''}")
    sys_t = sorted((k for k in st if k.startswith("systemTemperature")), key=lambda k: int(k[17:] or 0))
    if sys_t:
        print("  system temperatures:")
        for k in sys_t:
            i = k[17:]
            label = names.get(f"systemSensorName{i}") or names.get(f"sysName{i}") or st[k][1] or f"#{i}"
            print(f"    {label:22} {st[k][0]}")
    apts = [st[k][0] for k in st if k.startswith("apartmentTemperature")]
    if apts:
        print(f"  apartment temperatures: {', '.join(a for a in apts if a not in ('', '0'))[:200]}")
    r, ms = fetch(box, "obix/alarm", auth_kind)
    print(f"\nobix/alarm: HTTP {r.status_code} in {ms} ms, {r.text.count('<obj')} object(s)")
    print("\nOK: the portal can read this box from beverly-pi.")


if __name__ == "__main__":
    main()
