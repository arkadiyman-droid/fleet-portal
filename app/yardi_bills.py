"""
app/yardi_bills.py — utility bills from a Yardi payables export

Yardi Voyager, Payable Analytics, Report Type "Expense Distribution",
Property ".all", Account 512-010^512-020^512-030^512-040^512-050 (electric,
gas, heating oil, water & sewer, sewer), one calendar year per file, Excel.
Every line is one payable detail; a voided check shows as a minus line and
the reissue as a new line, so adding the lines up gives what was owed.

What the lines carry (the user's own export, 2026-11-27):
  Invoice #  "<utility account number>-<Yardi batch code>", e.g.
             "15912-81000-7-HBB26102007499"; deposits "...-deposit-ZHX..."
  Notes      "<what> <MM/DD/YYYY>-<MM/DD/YYYY> <service address>", e.g.
             "Electricity 08/31/2026-09/30/2026 20 RICHMAN PLZ",
             "Vacant Electric 09/03/2019-09/29/2019 [DI-6028] 6602 District..."
There is no usage (kWh, therms) in Yardi, only money: usage comes from the
utility bills Conservice keeps for each payable (see "usage" below).

How lines become accounts and bills:
  * one account = utility (payee) + account number + commodity + the
    building the service address names. A supplier contract (Direct
    Energy) covering three buildings becomes three accounts, so cost
    lands on the right building.
  * apartment lines (vacant units, "Master Unit", "# 5F", "APT 4") are
    rolled into one account per property, utility, commodity and kind;
    they are apartments' own electric, not the plant.
  * deposits are money held by the utility, not use: counted, never billed.
  * a bill = the lines of one account for one service period, netted.
    Lines with no service period fall back to Yardi's posting month.
  * Yardi properties are matched to the portal's properties by the
    service addresses (number + street against the buildings), then by
    name; what matches neither is listed, never guessed.

  parse_line(row)          one export row -> dict, or None
  street_key(address)      (number, street stem) for matching, or None
  read_xlsx(path)          the export's rows (needs openpyxl; run locally)
  import_lines(lines, ...) replace the Yardi accounts and bills
  summary()                what the Utilities page shows
"""
import datetime as _dt
import re
from collections import defaultdict

GL_COMMODITY = {"512-010": "electric", "512-020": "gas", "512-030": "oil", "512-040": "water", "512-050": "sewer"}
SUPPLIERS = ("direct energy", "nrg", "constellation", "reliant", "pro energy", "agway", "hudson energy",
             "spark energy", "ambit", "engie", "calpine", "freepoint", "mxenergy", "just energy")

_PERIOD = re.compile(r"(\d{1,2}[/.]\d{1,2}[/.]\d{2,4})\s*(?:-|to)\s*(\d{1,2}[/.]\d{1,2}[/.]\d{2,4})", re.I)
_NOTE_ACCT = re.compile(r"\b(?:acct|account)\s*(?:#|no\.?)?\s*:?\s*#?\s*([0-9][0-9 -]{3,}[0-9])(?=\s+\d{1,2}[/.]|\s*$|\s+[A-Za-z]|/)", re.I)
_INV_MONTH = re.compile(r"^(\d[\d -]{5,}\d)[- ]\d{1,2}/\d{2,4}$")
_BATCH = re.compile(r"-[A-Z]{3}\d{6,}\s*$")
_UNIT = re.compile(r"(#|\bAPT\b|\bUNIT\s*\d|\bSUITE\b|\bFL\s*\d|\s\d{1,2}[A-Z]{1,2}$|\s[A-Z]\d{1,2}$|\[)", re.I)
_DIR = {"north": "n", "south": "s", "east": "e", "west": "w", "n": "n", "s": "s", "e": "e", "w": "w"}
_TYPES = {"st", "street", "ave", "av", "avenue", "blvd", "boulevard", "pl", "plac", "place", "plz", "plaza", "dr",
          "drive", "rd", "road", "ln", "lane", "ct", "court", "ter", "terrace", "pkwy", "parkway", "cir", "circle",
          "way", "hwy", "sq", "square", "trl", "row", "walk"}


def _date(s):
    s = (s or "").replace(".", "/")
    for f in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return _dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def _as_date(v):
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str):
        for f in ("%m/%d/%Y", "%Y-%m-%d", "%m/%Y"):
            try:
                return _dt.datetime.strptime(v.strip()[:10], f).date()
            except ValueError:
                pass
    return None


def account_number(invoice):
    """The utility's account number out of Yardi's invoice number, or None
    when the invoice number is not '<account>-<batch>'."""
    s = str(invoice or "").strip()
    m = _INV_MONTH.match(s)
    if m:                                                   # NYC Water Board: "1000785042001-12/22"
        return m.group(1).strip()
    if not _BATCH.search(s):
        return None
    s = _BATCH.sub("", s)
    s = re.sub(r"-deposit$", "", s, flags=re.I)
    return s.strip("- ") or None


def street_key(address):
    """(number, street stem) or None: "725 GARDENST ENTM" -> (725, "garden"),
    "801 SOUTH ST MDRP" -> (801, "s"), "195 1st St HSE 2" -> (195, "1")."""
    s = re.sub(r"\[[^\]]*\]", " ", str(address or ""))
    s = s.split("#")[0].split(",")[0]
    m = re.match(r"^\s*(\d+)[A-Za-z]?(?:\s*-\s*(\d+))?\s+(.+)$", s)
    if not m:
        return None
    num = int(m.group(1))
    if m.group(2) and len(m.group(2)) == 2 and num >= 100 and int(m.group(2)) < num:
        num = f"{num}-{m.group(2)}"                         # Queens: "125-30 Sutphin Blvd" is one number
    words = re.findall(r"[A-Za-z0-9]+", m.group(3).lower())
    if not words:
        return None
    w = words[0]
    if w in _DIR and len(words) > 1 and words[1] not in _TYPES:
        words = words[1:]                                   # "E 104th St": the direction is not the name
        w = words[0]
    w = _DIR.get(w, w)
    w = re.sub(r"^(\d+)(st|nd|rd|th)$", r"\1", w)
    if len(w) > 4 and not w.isdigit():
        for t in ("street", "st", "ave", "av"):              # "GARDENST", "LOTTAVE": glued street types
            if w.endswith(t) and len(w) - len(t) >= 4:
                w = w[: -len(t)]
                break
    return num, w[:5]


def building_key(name):
    """(low, high, stem) for a portal building name or address, or None."""
    from app import benchmark as bm
    p = bm.parse_address(name or "")
    if not p:
        return None
    k = street_key(f"{p[0][0]} {p[1]}")
    if not k:
        return None
    return (p[0][0], p[0][0], k[1]) if isinstance(p[0][0], str) else (p[0][0], p[0][1], k[1])


def _covers(b, k):
    """A building key (low, high, stem) covers a service address key (number, stem)."""
    if b[2] != k[1]:
        return False
    if isinstance(k[0], str) or isinstance(b[0], str):
        return b[0] == k[0]
    return b[0] <= k[0] <= b[1]


def kind_of(desc, address, payee):
    d, a = (desc or "").lower(), address or ""
    if "deposit" in d:
        return "deposit"
    if d.startswith("vacant"):
        return "vacant"
    if "master unit" in d or "unit electric" in d or _UNIT.search(a.strip()):
        return "unit"
    if "elemco" in (payee or "").lower() or re.search(r"\bfee\b|admin|refund|late payment|late charge|\bloan", d):
        return "fee"
    return "building"


def norm_account(a):
    """One account written two ways ("1001039490001", "1001-0394-9000-1") is one key."""
    return re.sub(r"[^0-9a-z]", "", (a or "").lower())


def plausible(start, end, period):
    """A service period that can be real: at most ~13 months long, ending no
    more than 4 months after Yardi's posting month and no more than 3 years
    before it (old catch-up bills are real)."""
    if (end - start).days > 400:
        return False
    if period and not (period - _dt.timedelta(days=3 * 366) <= end <= period + _dt.timedelta(days=124)):
        return False
    return True


def parse_line(r):
    """One export row (a dict keyed by the export's column titles) -> dict, or
    None for headings, totals and blank rows."""
    prop = (r.get("Property") or "")
    amt = r.get("Amount")
    if not isinstance(prop, str) or not prop.strip() or amt is None or isinstance(amt, str):
        return None
    note = re.sub(r"\s+", " ", str(r.get("Notes") or "")).strip()
    m = _PERIOD.search(note)
    start = end = None
    desc, addr = note, ""
    if m:
        start, end = _date(m.group(1)), _date(m.group(2))
        desc, addr = note[: m.start()].strip(" -"), note[m.end():].strip()
        addr = re.sub(r"^\[[^\]]*\]\s*", "", addr)
        addr = re.sub(r"\b(acct|account)\b.*$", "", addr, flags=re.I).strip()
        if start and end and end < start:
            start, end = end, start
    period = _as_date(r.get("Period"))
    if start and end and not plausible(start, end, period):
        start = end = None                                  # a typo ("09/30/2065"): use the posting month
    payee = str(r.get("Payee Name") or r.get("Payee Code") or "").strip()
    invoice = str(r.get("Invoice #") or "").strip()
    acct = account_number(invoice)
    if not acct:
        n = _NOTE_ACCT.search(note)
        acct = n.group(1).strip() if n else None
    kind = kind_of(desc if m else note, addr, payee)
    if kind == "building" and not addr and not acct:
        kind = "other"                                      # nothing says which meter: property level
    return {
        "gl": str(r.get("Account Code") or "").strip(), "property": prop.strip(), "payee": payee,
        "invoice": invoice, "account": acct, "control": str(r.get("Payable Control") or "").strip(),
        "amount": float(amt), "period": period, "start": start, "end": end, "desc": desc, "address": addr,
        "kind": kind,
    }


def read_xlsx(path):
    """The rows of one Expense Distribution export, as dicts. The account
    code is written once above its lines; it is carried down here."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    head, gl, out = None, None, []
    for row in ws.iter_rows(values_only=True):
        if head is None:
            if row and row[0] == "Account Code":
                head = [str(c or "").strip() for c in row]
            continue
        r = dict(zip(head, row))
        a = r.get("Account Code")
        if isinstance(a, str) and re.match(r"^\d{3}-\d{3}$", a.strip()):
            gl = a.strip()
        if r.get("Property"):
            r["Account Code"] = gl
            out.append(r)
    wb.close()
    return out


def _month_end(d):
    nxt = (d.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    return nxt - _dt.timedelta(days=1)


# ── matching Yardi's properties to the portal's ──────────────────────────────
def _letters(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def match_properties(lines, props):
    """{yardi code: (portal property id or None, how)}. `props` = [{"id",
    "name", "buildings": [(building id, name, address)]}]. Service addresses
    vote (a property wins with most of the matched money); a code nothing
    votes for may match a portal property whose name it starts."""
    keys = []
    for p in props:
        for bid, nm, ad in p["buildings"]:
            for t in (nm, ad):
                k = building_key(t)
                if k:
                    keys.append((k, p["id"], bid))
    votes = defaultdict(lambda: defaultdict(float))
    for ln in lines:
        k = street_key(ln["address"])
        if not k:
            continue
        for bk, pid, _ in keys:
            if _covers(bk, k):
                votes[ln["property"]][pid] += abs(ln["amount"]) or 0.01
                break
    out = {}
    for code in {ln["property"] for ln in lines}:
        v = votes.get(code)
        if v:
            pid, top = max(v.items(), key=lambda kv: kv[1])
            if top >= 0.6 * sum(v.values()):
                out[code] = (pid, "address")
                continue
        c = _letters(code)
        named = [p["id"] for p in props if len(c) >= 4 and _letters(p["name"]).startswith(c)]
        out[code] = (named[0], "name") if len(named) == 1 else (None, None)
    return out


def match_building(address, buildings):
    """The building id a service address names, among one property's
    [(id, name, address)], or None."""
    k = street_key(address)
    if not k:
        return None
    for bid, nm, ad in buildings:
        for t in (nm, ad):
            b = building_key(t)
            if b and _covers(b, k):
                return bid
    return None


# ── lines -> accounts and bills ──────────────────────────────────────────────
def build(lines, pmap, blds_by_prop):
    """The accounts and bills the lines make, as plain dicts (no database).
    pmap = match_properties(); blds_by_prop = {portal property id: [(id, name, address)]}."""
    accounts, deposits, skipped = {}, defaultdict(float), 0
    for ln in lines:
        com = GL_COMMODITY.get(ln["gl"])
        if not com:
            skipped += 1
            continue
        if ln["kind"] == "deposit":
            deposits[ln["property"]] += ln["amount"]
            continue
        pid = (pmap.get(ln["property"]) or (None,))[0]
        roll = ln["kind"] in ("vacant", "unit", "fee", "other")
        bid = None if roll else match_building(ln["address"], blds_by_prop.get(pid, []))
        sk = street_key(ln["address"]) if not roll else None
        if roll:
            key = ("roll", ln["property"], ln["payee"].lower(), com, ln["kind"])
        else:
            key = ("acct", ln["property"], ln["payee"].lower(), norm_account(ln["account"]), com,
                   bid or sk or ln["address"].upper())
        a = accounts.get(key)
        if a is None:
            a = accounts[key] = {
                "key": "|".join(str(x) for x in key)[:300], "utility": ln["payee"][:80], "commodity": com,
                "account_number": None if roll else ln["account"], "kind": ln["kind"] if roll else "building",
                "service_address": ({"vacant": "Vacant apartments", "unit": "Apartments", "fee": "Fees and refunds",
                                     "other": "No meter named"}.get(ln["kind"], "Apartments") if roll
                                    else (ln["address"][:300] or None)),
                "ext_property": ln["property"], "property_id": pid, "building_id": bid,
                "supplier": any(s in ln["payee"].lower() for s in SUPPLIERS), "bills": {}, "n_lines": 0}
        a["n_lines"] += 1
        if roll or not (ln["start"] and ln["end"]):
            d = ln["end"] or ln["period"]
            if d is None:
                skipped += 1
                continue
            start, end = d.replace(day=1), _month_end(d)
        else:
            start, end = ln["start"], ln["end"]
        b = a["bills"].setdefault((start, end), {"start": start, "end": end, "cost": 0.0, "inv": defaultdict(float),
                                                 "lines": 0})
        b["cost"] += ln["amount"]
        b["inv"][ln["invoice"] or ln["control"]] += ln["amount"]
        b["lines"] += 1
    for a in accounts.values():
        for b in a["bills"].values():
            b["flags"] = flags(b, a)
        a["bills"] = {k: b for k, b in a["bills"].items() if abs(b["cost"]) >= 0.005}
    return {"accounts": [a for a in accounts.values() if a["bills"]], "deposits": dict(deposits), "skipped": skipped}


def flags(bill, account):
    """What looks wrong about one bill: the same period paid on two invoices
    for about the same money, and both still counted (a double payment?).
    A rebill that voids the first invoice nets back to one and is fine."""
    if account["kind"] != "building":
        return None
    pos = sorted((v for v in bill["inv"].values() if v > 1), reverse=True)
    if (len(pos) >= 2 and abs(pos[0] - pos[1]) <= 0.02 * pos[0] and pos[0] >= 50
            and bill["cost"] >= pos[0] + 0.9 * pos[1]):
        return "twice"
    return None


def import_lines(lines, source_ref="yardi", usage=None):
    """Replace every Yardi-sourced account and bill with what `lines` make,
    with usage from `usage` (usage_index / load_usage) when given; without
    it the bills carry money only. Building links a person set on a Yardi
    account (kept by the account's key) survive. Returns counts. App context
    assumed."""
    from app.extensions import db
    from app.models import Building, Property, UtilityAccount, UtilityAccountBuilding, UtilityBill
    props = []
    blds = defaultdict(list)
    for b in Building.query.join(Property).filter(Building.is_active.is_(True), Property.is_active.is_(True)).all():
        blds[b.property_id].append((b.id, b.name, b.address))
    for p in Property.query.filter_by(is_active=True).all():
        props.append({"id": p.id, "name": p.name, "buildings": blds.get(p.id, [])})
    pmap = match_properties(lines, props)
    res = build(lines, pmap, blds)
    used = attach_usage(res["accounts"], usage) if usage else None
    old = {a.ext_key: a for a in UtilityAccount.query.filter_by(source="yardi").all()}
    manual = {}
    for a in old.values():
        if a.links_set_by:
            manual[a.ext_key] = ([(x.building_id, x.share) for x in a.buildings], a.links_set_by, a.links_set_at)
        db.session.delete(a)
    db.session.flush()
    now = _dt.datetime.utcnow()
    n_bills = 0
    for a in res["accounts"]:
        ua = UtilityAccount(utility=a["utility"], commodity=a["commodity"], account_number=a["account_number"],
                            service_address=a["service_address"], source="yardi", ext_key=a["key"],
                            ext_property=a["ext_property"], property_id=a["property_id"], kind=a["kind"],
                            supplier=a["supplier"], is_active=True)
        db.session.add(ua)
        db.session.flush()
        if a["key"] in manual:
            links, ua.links_set_by, ua.links_set_at = manual[a["key"]]
        else:
            links = [(a["building_id"], 1.0)] if a["building_id"] else []
        for bid, share in links:
            db.session.add(UtilityAccountBuilding(account_id=ua.id, building_id=bid, share=share))
        rows = []
        for b in a["bills"].values():
            rows.append(dict(account_id=ua.id, period_start=b["start"], period_end=b["end"],
                             cost_cents=int(round(b["cost"] * 100)), source="yardi", source_ref=source_ref[:300],
                             lines=b["lines"], invoices=len([v for v in b["inv"].values() if abs(v) >= 0.005]),
                             flags=b["flags"], imported_at=now, estimated=False,
                             usage=b.get("usage"), unit=b.get("unit")))
        if rows:
            db.session.bulk_insert_mappings(UtilityBill, rows)
            n_bills += len(rows)
    db.session.commit()
    unmatched = sorted(c for c, (pid, _) in pmap.items() if not pid)
    return {"lines": len(lines), "accounts": len(res["accounts"]), "bills": n_bills, "skipped": res["skipped"],
            "deposits": round(sum(res["deposits"].values()), 2),
            "usage": ({"bills": used["bills"], "with_usage": used["with_usage"], "flat": used["flat"],
                       "dropped": len(used["dropped"])}
                      if used else None),
            "matched": {c: pid for c, (pid, _) in pmap.items() if pid}, "unmatched": unmatched,
            "how": {c: how for c, (_, how) in pmap.items()}}


# ── usage, from the bill data Conservice keeps ───────────────────────────────
# Conservice pays the utility bills and keeps each one's line items: one
# record per Yardi invoice, [account, batch, [[utility id, service address,
# account type, amount, usage, unit], ...]], pulled read-only from its DNA
# site (yardi-utilities/raw/conservice_usage.json). A bill with supply and
# delivery on it lists the same kWh twice (electric + elecsupp), an ESCO's
# own invoice repeats the utility's kWh, and sewer repeats water's gallons:
# only the utility's delivery items are counted, and an ESCO's account
# (supplier=True) gets none, so nothing is counted twice.
# A few utilities' records are off by ~1000x (36M kWh on a $5,497 Central
# Hudson bill, San Antonio water in gallons x1000): usage whose own items
# price it below FLOOR $ per unit is a data error, dropped and counted.
DELIVERY = {"electric": {"electric", "elec2"}, "gas": {"gas", "gas2", "gas3"}, "water": {"water", "water2"}}
UNITS = {"kwh": ("kWh", 1.0), "therms": ("therm", 1.0), "gal": ("kgal", 0.001)}
FLOOR = {"kWh": 0.02, "therm": 0.05, "kgal": 0.5}


def batch_of(invoice):
    """Yardi's batch code out of the invoice number ("...-HBB26102007499"), or None."""
    m = _BATCH.search(str(invoice or ""))
    return m.group(0).strip("- \r\n") if m else None


def usage_index(records):
    """Conservice records -> {(account, batch): items}."""
    return {(norm_account(acct), str(batch).strip()): items for acct, batch, items in records if items}


def load_usage(path):
    import gzip
    import json
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        return usage_index(json.load(f))


def invoice_usage(items, commodity, address):
    """(usage, unit, amount) that one invoice's delivery items show for
    this commodity and service address, or None. An invoice that covers
    several buildings (one account, several addresses) gives each its own.
    A water invoice that charges money but shows no usage at all is a flat
    charge (NYC's Multi-Family Conservation Program bills per apartment, a
    year ahead): (0, "flat", amount). It has no usage to miss."""
    want = DELIVERY.get(commodity)
    if not want:
        return None
    its = [i for i in items if i[0] in want]
    if not its:
        return None
    if len({street_key(i[1]) or str(i[1]).upper() for i in its}) > 1:
        k = street_key(address)
        its = [i for i in its if k and street_key(i[1]) == k]
        if not its:
            return None
    amount = round(sum(float(i[3] or 0) for i in its), 2)
    if commodity == "water" and amount > 0 and not any(i[4] for i in its):
        return 0.0, "flat", amount
    its = [i for i in its if i[4] is not None and str(i[5] or "").strip().lower() in UNITS]
    if not its:
        return None
    units = {UNITS[str(i[5]).strip().lower()] for i in its}
    if len(units) != 1:
        return None
    unit, f = units.pop()
    return round(sum(float(i[4]) for i in its) * f, 3), unit, amount


def attach_usage(accounts, index):
    """Put usage on the building bills build() made. A bill paid on two
    invoices (a duplicate, a rebill) shows its usage on both: the larger one
    is the bill's, never the sum. A bill with only flat charges gets unit
    "flat" and no usage. Returns counts and the dropped data errors."""
    out = {"bills": 0, "with_usage": 0, "flat": 0, "dropped": []}
    for a in accounts:
        if a["kind"] != "building" or a["supplier"]:
            continue
        for b in a["bills"].values():
            out["bills"] += 1
            best, flat = None, False
            for inv, amt in b["inv"].items():
                if amt <= 0.005:
                    continue
                items = index.get((norm_account(account_number(inv)), batch_of(inv) or ""))
                u = invoice_usage(items, a["commodity"], a["service_address"]) if items else None
                if u and u[1] == "flat":
                    flat = True
                    continue
                if not u or u[0] <= 0:
                    continue
                if u[2] / u[0] < FLOOR[u[1]]:
                    out["dropped"].append({"invoice": inv, "address": a["service_address"], "usage": u[0],
                                           "unit": u[1], "amount": u[2]})
                    continue
                if best is None or u[0] > best[0]:
                    best = u
            if best:
                b["usage"], b["unit"] = best[0], best[1]
                out["with_usage"] += 1
            elif flat:
                b["unit"] = "flat"
                out["flat"] += 1
    return out


# ── moving parsed lines between machines ─────────────────────────────────────
def dump_lines(lines, path):
    """Parsed lines -> gzipped JSON (the Pi has no openpyxl; parse where the
    export was downloaded, import on the server)."""
    import gzip
    import json

    def enc(v):
        return v.isoformat() if isinstance(v, _dt.date) else v
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump([{k: enc(v) for k, v in ln.items()} for ln in lines], f)


def load_lines(path):
    import gzip
    import json
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rows = json.load(f)
    for r in rows:
        for k in ("period", "start", "end"):
            if r.get(k):
                r[k] = _dt.date.fromisoformat(r[k])
    return rows


# ── what the Utilities page shows ────────────────────────────────────────────
GROUP = {"electric": "electric", "gas": "gas", "oil": "oil", "water": "water", "sewer": "water"}


USE_UNIT = {"electric": "kWh", "gas": "therm", "water": "kgal"}
COVER_MIN = 0.9          # a price per unit only where usage covers this share of the utility's own bills


def _usage(rows, ref, prev, yr):
    """Usage per property (electric kWh, gas therms, water kgal) for the
    reference year and the one before, the all-in price per unit and per
    apartment. Each row gets r["use"]; returns the portfolio totals, or None
    when no bill has usage. App context assumed.
      all-in cost  = everything the property paid for that fuel in the year,
                     net: supply, sewer, and lines Yardi re-entered with no
                     meter named (a voided and reissued payment); apartments'
                     own electric is not the plant and is left out.
      cover        = share of the utility's own bills that carry usage or
                     are flat charges with none, each account netted over
                     the year first (a payment voided later in the year,
                     or a void of last year's, adds nothing)."""
    from sqlalchemy import case, func
    from app.extensions import db
    from app.models import UtilityAccount, UtilityBill
    if not ref:
        return None
    covered = UtilityBill.usage.isnot(None) | (UtilityBill.unit == "flat")
    q = (db.session.query(UtilityAccount.id, UtilityAccount.property_id, UtilityAccount.commodity,
                          UtilityAccount.supplier, UtilityAccount.kind, yr,
                          func.sum(UtilityBill.cost_cents),
                          func.sum(case((covered, UtilityBill.cost_cents), else_=0)),
                          func.sum(UtilityBill.usage))
         .join(UtilityBill, UtilityBill.account_id == UtilityAccount.id)
         .filter(UtilityAccount.source == "yardi", UtilityAccount.kind.in_(("building", "other", "fee")),
                 UtilityAccount.property_id.isnot(None), yr.in_([ref, prev] if prev else [ref]))
         .group_by(UtilityAccount.id, UtilityAccount.property_id, UtilityAccount.commodity,
                   UtilityAccount.supplier, UtilityAccount.kind, yr))
    acc = defaultdict(lambda: {"usage": 0.0, "all": 0.0, "own": 0.0, "cov": 0.0})
    any_use = False
    for _aid, pid, com, sup, kind, y, cents, cov_cents, used in q:
        g = GROUP.get(com, com)
        if g not in USE_UNIT:
            continue
        x = acc[(pid, int(y), g)]
        x["all"] += (cents or 0) / 100
        if kind == "building" and not sup and com != "sewer":
            net = max((cents or 0) / 100, 0.0)
            x["own"] += net
            x["cov"] += min(max((cov_cents or 0) / 100, 0.0), net)
        if used:
            x["usage"] += used
            any_use = True
    if not any_use:
        for r in rows:
            r["use"] = {}
        return None
    tot = {g: {"ref": 0.0, "all": 0.0, "props": 0} for g in USE_UNIT}
    for r in rows:
        r["use"] = {}
        for g in USE_UNIT:
            a, p = acc.get((r["id"], ref, g)), acc.get((r["id"], prev, g))
            if not a or not a["usage"]:
                continue
            cover = a["cov"] / a["own"] if a["own"] > 0 else None
            full = cover is not None and cover >= COVER_MIN
            pfull = p and p["usage"] and p["own"] > 0 and p["cov"] / p["own"] >= COVER_MIN
            r["use"][g] = {
                "ref": a["usage"], "unit": USE_UNIT[g], "cover": cover,
                "price": a["all"] / a["usage"] if full else None,
                "per_apt": a["usage"] / r["units"] if full and r["units"] else None,
                "change": (a["usage"] / p["usage"] - 1) if full and pfull else None}
            if full:
                t = tot[g]
                t["ref"] += a["usage"]
                t["all"] += a["all"]
                t["props"] += 1
    for g, t in tot.items():
        t["unit"] = USE_UNIT[g]
        t["price"] = t["all"] / t["ref"] if t["ref"] else None
    return tot


def summary(today=None):
    """Yardi costs per portal property and year, the building meters behind
    them, the bills to check, and the Yardi properties the portal does not
    have. None when nothing was imported. App context assumed."""
    from sqlalchemy import func
    from app.extensions import db
    from app.models import Building, Property, UtilityAccount, UtilityAccountBuilding, UtilityBill
    today = today or _dt.date.today()
    n = UtilityAccount.query.filter_by(source="yardi").count()
    if not n:
        return None
    yr = func.extract("year", UtilityBill.period_end)
    q = (db.session.query(UtilityAccount.property_id, UtilityAccount.ext_property, UtilityAccount.commodity,
                          UtilityAccount.kind, yr, func.sum(UtilityBill.cost_cents), func.count(UtilityBill.id))
         .join(UtilityBill, UtilityBill.account_id == UtilityAccount.id)
         .filter(UtilityAccount.source == "yardi")
         .group_by(UtilityAccount.property_id, UtilityAccount.ext_property, UtilityAccount.commodity,
                   UtilityAccount.kind, yr))
    span = (db.session.query(func.min(UtilityBill.period_end), func.max(UtilityBill.period_end),
                             func.max(UtilityBill.imported_at), func.count(UtilityBill.id))
            .filter(UtilityBill.source == "yardi").one())
    last = span[1]
    years = sorted({y for y in range((span[0] or today).year, (last or today).year + 1)})
    years = [y for y in years if y >= (last or today).year - 3]
    props = {p.id: p for p in Property.query.all()}
    per = {}
    other = defaultdict(lambda: {"total": 0.0, "years": defaultdict(float)})
    for pid, code, com, kind, y, cents, nb in q:
        y = int(y)
        cost = (cents or 0) / 100
        if not pid:
            o = other[code]
            o["years"][y] += cost
            o["total"] += cost
            continue
        r = per.setdefault(pid, {"id": pid, "name": props[pid].name if pid in props else str(pid),
                                 "units": props[pid].unit_count if pid in props else None,
                                 "codes": set(), "years": defaultdict(float),
                                 "by": defaultdict(lambda: defaultdict(float)), "apts": defaultdict(float)})
        r["codes"].add(code)
        r["years"][y] += cost
        r["by"][y][GROUP.get(com, com)] += cost
        if kind in ("unit", "vacant"):
            r["apts"][y] += cost
    full = [y for y in years if last and (y < last.year or (last.month == 12 and last.day == 31))]
    ref = full[-1] if full else (years[-1] if years else None)
    prev = ref - 1 if ref else None
    rows = []
    for r in per.values():
        r["codes"] = sorted(r["codes"])
        r["ref"] = r["years"].get(ref, 0.0)
        r["change"] = ((r["ref"] / r["years"][prev] - 1) if prev and r["years"].get(prev) else None)
        r["per_unit"] = (r["ref"] / r["units"]) if r["units"] and r["ref"] else None
        r["split"] = dict(r["by"].get(ref, {}))
        r["apt_share"] = (r["apts"].get(ref, 0.0) / r["ref"]) if r["ref"] else None
        rows.append(r)
    rows.sort(key=lambda r: -r["ref"])
    use_tot = _usage(rows, ref, prev, yr)
    # the building meters behind each property: the latest 12 months
    # the last month whose bills are all in: two months before the newest
    # import (a September bill is posted in October or November)
    anchor = min(last or today, (span[2].date() if span[2] else today))
    cut_hi = (anchor.replace(day=1) - _dt.timedelta(days=1)).replace(day=1) - _dt.timedelta(days=1)
    cut_lo = (cut_hi.replace(day=1) - _dt.timedelta(days=330)).replace(day=1)
    aq = (db.session.query(UtilityAccount, func.sum(UtilityBill.cost_cents), func.count(UtilityBill.id),
                           func.sum(UtilityBill.usage), func.max(UtilityBill.unit))
          .join(UtilityBill, UtilityBill.account_id == UtilityAccount.id)
          .filter(UtilityAccount.source == "yardi", UtilityAccount.property_id.isnot(None),
                  UtilityBill.period_end >= cut_lo, UtilityBill.period_end <= cut_hi)
          .group_by(UtilityAccount.id))
    links = defaultdict(list)
    for x in (db.session.query(UtilityAccountBuilding.account_id, Building.name)
              .join(Building, Building.id == UtilityAccountBuilding.building_id)
              .join(UtilityAccount, UtilityAccount.id == UtilityAccountBuilding.account_id)
              .filter(UtilityAccount.source == "yardi")):
        links[x[0]].append(x[1])
    meters = defaultdict(list)
    for a, cents, nb, used, unit in aq:
        meters[a.property_id].append({
            "id": a.id, "utility": a.utility, "commodity": a.commodity, "kind": a.kind, "number": a.account_number,
            "address": a.service_address, "buildings": links.get(a.id, []), "supplier": a.supplier,
            "cost": (cents or 0) / 100, "bills": nb, "usage": used, "unit": unit})
    for pid in meters:
        meters[pid].sort(key=lambda m: -m["cost"])
    for r in rows:
        ms = meters.get(r["id"], [])
        r["meters"] = ms
        bm_cost = sum(m["cost"] for m in ms if m["kind"] == "building")
        r["meters_linked"] = (sum(m["cost"] for m in ms if m["kind"] == "building" and m["buildings"]) / bm_cost
                              if bm_cost else None)
    checks = []
    for b, a in (db.session.query(UtilityBill, UtilityAccount)
                 .join(UtilityAccount, UtilityAccount.id == UtilityBill.account_id)
                 .filter(UtilityBill.source == "yardi", UtilityBill.flags.isnot(None))
                 .order_by(UtilityBill.cost_cents.desc())):
        checks.append({"flag": b.flags, "cost": b.cost_cents / 100, "start": b.period_start, "end": b.period_end,
                       "invoices": b.invoices, "utility": a.utility, "number": a.account_number,
                       "address": a.service_address, "commodity": a.commodity,
                       "property": props[a.property_id].name if a.property_id in props else a.ext_property})
    others = sorted(({"code": c, "total": o["total"], "ref": o["years"].get(ref, 0.0)} for c, o in other.items()),
                    key=lambda o: -o["total"])
    return {
        "accounts": n, "bills": span[3], "first": span[0], "last": last, "imported": span[2],
        "years": years, "ref": ref, "prev": prev, "rows": rows, "checks": checks,
        "check_total": sum(c["cost"] / 2 for c in checks),
        "others": others, "others_ref": sum(o["ref"] for o in others),
        "portal_ref": sum(r["ref"] for r in rows), "cut_lo": cut_lo, "cut_hi": cut_hi,
        "totals": {yy: sum(r["years"].get(yy, 0.0) for r in rows) for yy in years},
        "usage": use_tot,
    }
