"""
app/directory.py — the manager directory, read from the violations portal.

PATHSCO Violations (a separate application) already records every property
with its property manager, regional manager and site office, by email. This
module reads that list (GET /api/v1/directory with a bearer token), lines its
properties up with this portal's own, and works out who should be able to
watch what — as SUGGESTIONS. Nothing here gives anybody access: the
administrator approves on the Directory page (routes.directory_page).

Two things have to be matched by hand once, because the two systems name
things differently ("Bedell" / "Bedell Terrace", "Betty" + "Medgar Evers" /
"Betty and Medgar"): which directory property is which fleet property.
suggest_links() proposes; the administrator confirms. Several directory
properties may point at one fleet property.

Everything below fetch() is pure.
"""
import re

import requests

# ── reading the directory ─────────────────────────────────────────────────────

def fetch(base_url: str, token: str, timeout=20) -> dict:
    """The directory as the violations portal serves it:
    {"org", "generated_at", "properties": [{id, name, region, unit_count,
    pm_name, pm_email, rpm_name, rpm_email, site_email, buildings: [{address,
    city, state, zip, bin}], open_violations, overdue_obligations}]}."""
    r = requests.get(base_url.rstrip("/") + "/api/v1/directory", timeout=timeout,
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code == 401:
        raise RuntimeError("the violations portal refused the token (401) — check DIRECTORY_TOKEN")
    if r.status_code == 404:
        raise RuntimeError("the violations portal has no /api/v1/directory yet (404) — it needs its update")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not isinstance(data.get("properties"), list):
        raise RuntimeError("the violations portal's reply is not a directory")
    return data


# ── emails ────────────────────────────────────────────────────────────────────

_EMAIL = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")


def norm_email(e) -> str:
    return str(e or "").strip().lower()


def email_problem(e, domains=()) -> str:
    """'' when the address can be used; otherwise what is wrong with it, in
    words. `domains` is the allowed list (company domains); empty = any."""
    e = norm_email(e)
    if not e:
        return "no email"
    if not _EMAIL.match(e):
        return "not a valid email address"
    dom = e.rsplit("@", 1)[1]
    if domains and dom not in {d.lower() for d in domains}:
        return f"not a company address (@{dom})"
    return ""


# ── lining properties up ──────────────────────────────────────────────────────

_NAME_STOP = {"the", "and", "at", "of", "apartments", "apartment", "apts", "apt", "houses", "house", "residences",
              "lp", "pag", "nycha", "inc", "llc", "i", "ii", "iii", "1", "2", "3"}
_STREET_TYPE = {"st", "street", "ave", "avenue", "av", "pl", "place", "blvd", "boulevard", "dr", "drive", "ln", "lane",
                "rd", "road", "plz", "plaza", "ct", "court", "circ", "circle", "cir", "way", "ter", "pkwy", "parkway"}
_DIR = {"north": "n", "south": "s", "east": "e", "west": "w", "n": "n", "s": "s", "e": "e", "w": "w"}
_ZONE = {"zone", "nw", "sw", "ne", "se", "z1", "z2", "car", "apt", "bldg", "building"}
_PREFIX = {"ind", "wil", "rp"}                 # this portal's own building-name prefixes


def name_tokens(name) -> frozenset:
    """The distinctive words of a property name: 'Baisley Park Gardens' ->
    {baisley, park, gardens}; 'DCA1' -> {dca}; 'Morris I, LP' -> {morris}."""
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", str(name or "").lower())     # dca1 -> dca 1
    words = re.sub(r"[^a-z0-9]+", " ", s).split()
    return frozenset(w for w in words if w not in _NAME_STOP)


def street(address) -> str:
    """The street an address is on, without number, type or wing, so
    '46 South 9TH St aka 451 Wythe Ave', '50 South 9 St' and 'S. 9th Street'
    all give 's 9', and '101 Terrace Ave' / '97-99-101 Terrace St' both give
    'terrace'. '' when there is no street in it (e.g. 'RP 10AH')."""
    s = str(address or "").lower()
    s = re.split(r"aka|a\.k\.a|\(", s)[0]                   # drop "aka ..." and "(...)" tails
    out = []
    for w in re.sub(r"[^a-z0-9]+", " ", s).split():
        m = re.fullmatch(r"(\d+)(st|nd|rd|th)", w)
        if m:
            out.append(m.group(1))                                   # 9th -> 9
        elif w in _STREET_TYPE:
            if out:
                break                                                # the name ends at its type
        elif w in _ZONE:
            continue
        elif not out and (w.isdigit() or w in _PREFIX or re.fullmatch(r"\d+[a-z]{1,2}", w)):
            continue                                                 # house numbers, 'Ind-1', 'RP 10AH'
        else:
            out.append(_DIR.get(w, w))
    while len(out) > 1 and out[-1] in ("n", "s", "e", "w"):          # a trailing direction is a wing ("85 Taylor N")
        out.pop()
    return " ".join(out)


def _streets(addresses) -> set:
    return {s for s in (street(a) for a in addresses) if s and not s.isdigit()}


def suggest_links(directory, fleet) -> dict:
    """Which fleet property each directory property most likely is.

    directory: [{"id", "name", "addresses": [...]}]; fleet: [{"id", "name",
    "addresses": [...]}] (a fleet building's name is its address).
    Returns {directory id: {"property_id", "score", "why", "confidence"}} for
    the ones with a believable match; the rest are left for a person. Scored
    on the name (distinctive words in common) and on shared streets."""
    fl = [{"id": f["id"], "name": f["name"], "tok": name_tokens(f["name"]), "st": _streets(f["addresses"])} for f in fleet]
    out = {}
    for d in directory:
        tok, st = name_tokens(d["name"]), _streets(d["addresses"])
        scored = []
        for f in fl:
            name = 0.0
            if tok & f["tok"]:
                name = 1.0 if tok == f["tok"] else len(tok & f["tok"]) / len(tok | f["tok"])
            shared = st & f["st"]
            addr = len(shared) / len(st) if st else 0.0
            if name or shared:
                scored.append((name + addr, name, len(shared), f))
        if not scored:
            continue
        scored.sort(key=lambda x: (-x[0], x[3]["name"]))
        best = scored[0]
        runner = scored[1][0] if len(scored) > 1 else 0.0
        total, name, n_shared, f = best
        # believable: a real name match, or streets in common; and clearly ahead of the next
        if total < 0.5 or (total - runner) < 0.2:
            continue
        why = []
        if name == 1.0:
            why.append("same name")
        elif name:
            why.append("similar name")
        if n_shared:
            why.append(f"{n_shared} street{'s' if n_shared != 1 else ''} in common")
        # "name": the names agree, so it is offered ready-ticked; "streets": only
        # the addresses overlap — shown as a hint and left for a person to pick.
        out[d["id"]] = {"property_id": f["id"], "score": round(total, 2), "why": ", ".join(why),
                        "confidence": "name" if name else "streets"}
    return out


# ── who should be able to watch what ──────────────────────────────────────────

ROLE_WORDS = {"pm": "property manager", "rpm": "regional manager", "site": "site office"}


def people(rows, domains=(), include_site=False) -> list:
    """The managers named on LINKED directory properties, one entry per email.

    rows: [{"name", "property_id" (fleet, or None when not linked), "pm_name",
    "pm_email", "rpm_name", "rpm_email", "site_email"}].
    Returns [{"email", "name", "roles": {"pm", "rpm", ...}, "property_ids":
    set, "from": [directory property names], "problem": '' or why the address
    cannot be used}], regional managers first, then by name."""
    by = {}
    for r in rows:
        if not r.get("property_id"):
            continue
        slots = [("pm", r.get("pm_name"), r.get("pm_email")), ("rpm", r.get("rpm_name"), r.get("rpm_email"))]
        if include_site:
            slots.append(("site", None, r.get("site_email")))
        for role, name, email in slots:
            e = norm_email(email)
            if not e:
                continue
            p = by.setdefault(e, {"email": e, "name": "", "roles": set(), "property_ids": set(), "from": []})
            if name and not p["name"]:
                p["name"] = str(name).strip()
            p["roles"].add(role)
            p["property_ids"].add(r["property_id"])
            if r["name"] not in p["from"]:
                p["from"].append(r["name"])
    out = []
    for p in by.values():
        p["problem"] = email_problem(p["email"], domains)
        p["name"] = p["name"] or p["email"].split("@")[0]
        p["role_words"] = ", ".join(ROLE_WORDS[r] for r in ("rpm", "pm", "site") if r in p["roles"])
        out.append(p)
    out.sort(key=lambda p: ("rpm" not in p["roles"], p["name"].lower()))
    return out


def access_plan(persons, users, grants) -> list:
    """What approving each person would change.

    users: {email: {"id", "active"}}; grants: {user id: {property id: source}}
    where source is 'directory' for a grant this page made and None for one
    made by hand. Adds to each person:
      state   "new" (no account) | "add" (account, missing some properties) |
              "ok" (nothing to do) | "blocked" (the address cannot be used) |
              "archived" (their account is switched off)
      add     property ids approving would grant
      drop    property ids granted from the directory that the directory no
              longer backs — offered for removal, never removed silently
    A grant made by hand is never offered for removal."""
    out = []
    for p in persons:
        u = users.get(p["email"])
        held = grants.get(u["id"], {}) if u else {}
        add = sorted(p["property_ids"] - set(held))
        drop = sorted(pid for pid, src in held.items() if src == "directory" and pid not in p["property_ids"])
        if p["problem"]:
            state = "blocked"
        elif u is None:
            state = "new"
        elif not u["active"]:
            state = "archived"
        elif add:
            state = "add"
        else:
            state = "ok"
        out.append(dict(p, user_id=u["id"] if u else None, state=state, add=add, drop=drop))
    return out


def orphans(persons, users, grants) -> list:
    """Accounts holding directory-made grants whose email is no longer in the
    directory at all: [{"user_id", "email", "drop": [property ids]}]."""
    listed = {p["email"] for p in persons}
    out = []
    for email, u in users.items():
        if email in listed:
            continue
        drop = sorted(pid for pid, src in grants.get(u["id"], {}).items() if src == "directory")
        if drop:
            out.append({"user_id": u["id"], "email": email, "drop": drop})
    return sorted(out, key=lambda o: o["email"])
