"""
app/legacy_settings.py — changing a 2010 oBIX box's settings from the portal

How these boxes take a write was read out of the gateway's own source (oBIX
server 2.6.3, Nov 2010), the PIC heat-control firmware, and the Windows client
that shipped with them. The boxes in the field run a later firmware (3.5.4),
so everything the box itself can tell us — which fields exist, whether each is
writable, its range — is taken from the box's live reply, not from that
source.

THE WRITE.  PUT http://box/obix/<object>, Digest login, Content-Type text/xml,
and a body of ONLY the changed items, one per line, no wrapper:

    <int name="outsideDaySetpoint" val="56"/>

  * By name, not position: items left out keep their values. Echoing a whole
    GET back does NOT work (its clock stamp is not writable and aborts the
    write half way), and an <obj> wrapper can retarget the write. So: only
    the changed items, never a wrapper.
  * The box answers 200 with the whole object, or 200 with an <err .../>. It
    CLAMPS an out-of-range value silently and turns text into 0, so every
    value is checked here first, and the reply is checked to carry exactly
    what was sent.
  * Each write makes the box rewrite whole blocks of its memory from the
    gateway's copy. If that copy were garbage (just rebooted, link to the
    controller board down) the write would store garbage — including the 24
    hourly targets that share a block. So the object is read first and the
    write is refused unless it reads back sane (sane()).
  * A wrong login level answers 401. Manager or Admin may write.
  * One connection at a time: nothing else should talk to the box during a
    write (routes.live_box_settings holds the poller's lock).

WHAT THE SETTINGS MEAN (PIC firmware 2.6.2, FC_HeatCtrl.c — later firmware
may differ in detail):
  * The temperature a building is heated to is the HOURLY TARGET table
    (obix/DITs: weekday / Saturday / Sunday x 24 hours), nudged a couple of
    degrees by the outdoor temperature when ditMode is 1.
  * insideDay/NightSetpoint are NOT that target: they are a heat-off point —
    heat stops once the coldest counted apartment reaches it.
  * outsideDay/NightSetpoint: heating is locked out while it is warmer than
    this outside. day/nightStartHour pick which of each pair applies.
  * winterStart / summerStart: the heating season.

This module is the box-facing half (parse, fields, sane, write); the form
rules are app/box_settings.py and the page is routes.live_box_settings.
"""
import re
import time

import requests
from requests.auth import HTTPDigestAuth

TIMEOUT = 20
SETTLE = 4                      # seconds the box gets to store a write before it is read back
SKIP = {"serverTime", "extOutsideTemp", "leadBoiler"}        # not settings: clock stamps, live values

_TAG = re.compile(r"<(\w+)\b([^<>]*?)/?>")
_ATTR = re.compile(r'([\w:]+)\s*=\s*"([^"]*)"')

DAYS = (("weekday", "Indoor target by hour, weekdays"), ("saturday", "Indoor target by hour, Saturday"),
        ("sunday", "Indoor target by hour, Sunday"))
# (object, item, label, kind, group, help)
SIMPLE = (
    ("emsSettings", "outsideDaySetpoint", "Outdoor cutoff, day", "deg", "Outdoor cutoff",
     "During the day, heating is locked out while it is warmer than this outside."),
    ("emsSettings", "outsideNightSetpoint", "Outdoor cutoff, night", "deg", "Outdoor cutoff", "The same, during the night."),
    ("emsSettings", "dayStartHour", "Day period starts", "hour", "Day and night", "From this hour the day settings apply."),
    ("emsSettings", "nightStartHour", "Night period starts", "hour", "Day and night", "From this hour the night settings apply."),
    ("emsSettings", "winterStartMonth", "Heating season starts, month", "month", "Heating season", ""),
    ("emsSettings", "winterStartDay", "Heating season starts, day", "day", "Heating season", ""),
    ("emsSettings", "summerStartMonth", "Heating season ends, month", "month", "Heating season", ""),
    ("emsSettings", "summerStartDay", "Heating season ends, day", "day", "Heating season", ""),
    ("emsSettings", "insideDaySetpoint", "Heat-off point, day", "deg", "Heat-off point",
     "Heat stops once the coldest counted apartment reaches this. It is not the temperature the building is "
     "heated to: the hourly targets below are."),
    ("emsSettings", "insideNightSetpoint", "Heat-off point, night", "deg", "Heat-off point", "The same, during the night."),
)
CHECKS_KEYS = {"day": ("emsSettings.dayStartHour", "emsSettings.nightStartHour"),
               "winter": ("emsSettings.winterStartMonth", "emsSettings.winterStartDay"),
               "summer": ("emsSettings.summerStartMonth", "emsSettings.summerStartDay")}


def parse(xml: str) -> dict:
    """{'root': 'obj' | 'err' | None, 'err': the box's own words or None,
    'items': {name: {val, writable, min, max}}} from an oBIX reply. Tolerant:
    the gateway's XML is line-oriented and not always well-formed."""
    root, err, items = None, None, {}
    for m in _TAG.finditer(xml or ""):
        tag, attrs = m.group(1), dict(_ATTR.findall(m.group(2)))
        if tag.lower() == "xml":
            continue
        if root is None:
            root = tag.lower()
            if root == "err":
                err = attrs.get("display") or attrs.get("is") or "the box refused"
            continue
        name = attrs.get("name")
        if name and "val" in attrs:
            items[name] = {"val": attrs["val"], "writable": attrs.get("writable", "").lower() == "true",
                           "min": _int(attrs.get("min")), "max": _int(attrs.get("max"))}
    return {"root": root, "err": err, "items": items}


def _int(v):
    try:
        f = float(str(v).strip())
        return int(f) if f == int(f) else None
    except (TypeError, ValueError):
        return None


def _hour_cell(h) -> str:
    return f"{(h % 12) or 12}{'a' if h < 12 else 'p'}"


def fields(objects: dict) -> list:
    """Form fields (app/box_settings.py) for what one box offers, from its
    live objects {'emsSettings': parse(...), 'DITs': parse(...)}. A field is
    offered only where the box lists it as writable with a whole-number
    value; its range is the box's own."""
    out = []

    def add(obj, item, label, kind, group, help_, cell=None, group_help=None):
        it = ((objects.get(obj) or {}).get("items") or {}).get(item)
        if not it or not it["writable"] or _int(it["val"]) is None:
            return
        out.append({"key": f"{obj}.{item}", "label": label, "kind": kind, "lo": it["min"], "hi": it["max"],
                    "value": _int(it["val"]), "help": help_, "group": group, "cell": cell, "group_help": group_help})

    for obj, item, label, kind, group, help_ in SIMPLE:
        add(obj, item, label, kind, group, help_)
    for stem, group in DAYS:
        for h in range(24):
            add("DITs", f"{stem}DITHour{h}", f"{group.split(', ')[-1].capitalize()} target, {_hour_words(h)}", "deg", group, "",
                cell=_hour_cell(h), group_help="the temperature the building is heated to in that hour")
    return out


def _hour_words(h) -> str:
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


def sane(obj: str, items: dict):
    """None when this object reads back like real settings, else why not. The
    guard against writing while the gateway's copy of the controller's memory
    is empty or garbage: a write would store that copy."""
    def val(name):
        return _int((items.get(name) or {}).get("val"))

    if obj == "emsSettings":
        need = {"insideDaySetpoint": (40, 99), "insideNightSetpoint": (40, 99), "dayStartHour": (0, 23),
                "nightStartHour": (0, 23), "winterStartMonth": (1, 12), "summerStartMonth": (1, 12)}
    elif obj == "DITs":
        need = {f"{stem}DITHour{h}": (40, 99) for stem, _g in DAYS for h in range(24)}
    else:
        return None
    seen = [n for n in need if n in items]
    if len(seen) < max(1, len(need) // 2):
        return "the box did not list the settings it should"
    bad = [n for n in seen if val(n) is None or not need[n][0] <= val(n) <= need[n][1]]
    if bad:
        return f"{len(bad)} of its values read back out of any sensible range (for example {bad[0]} = {items[bad[0]]['val']})"
    return None


def body(changes: dict) -> bytes:
    """The PUT body for {item: whole number}: only the changed items, one per
    line, no wrapper."""
    for item, v in changes.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", item) or isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"not a value that may be sent: {item} = {v!r}")
    return "".join(f'<int name="{item}" val="{v}"/>\r\n' for item, v in changes.items()).encode("ascii")


def session(user, password):
    s = requests.Session()
    s.auth = HTTPDigestAuth(user, password)
    return s


def read(s, url, objs=("emsSettings", "DITs"), pause=2.0, sleep=time.sleep) -> dict:
    """{'emsSettings': parse(...), ...} live from the box. Raises on a network
    failure or an HTTP status other than 200."""
    out = {}
    for i, obj in enumerate(objs):
        if i:
            sleep(pause)
        r = s.get(f"{url}/obix/{obj}", timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"the box answered HTTP {r.status_code} for {obj}")
        out[obj] = parse(r.text)
    return out


def about(s, url) -> dict:
    """{'level': the login's access level at this box, 'up_min': minutes since
    the box started (None if it does not say)} from obix/about."""
    import datetime as dt
    r = s.get(f"{url}/obix/about", timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"the box answered HTTP {r.status_code}")
    items = parse(r.text)["items"]

    def when(name):
        try:
            return dt.datetime.fromisoformat((items.get(name) or {}).get("val", "").replace("Z", "+00:00"))
        except ValueError:
            return None
    now, boot = when("serverTime"), when("serverBootTime")
    return {"level": (items.get("accessLevel") or {}).get("val", ""),
            "up_min": (now - boot).total_seconds() / 60 if now and boot else None}


MIN_UP_MIN = 3                  # a box that started less than this ago is not written to


def write(s, url, obj, changes, expect, sleep=time.sleep) -> dict:
    """Change items of ONE object on one box, the careful way. `changes` and
    `expect` are {item: whole number}; `expect` is what the person was shown.
    Returns {'ok', 'sent', 'confirmed', 'detail', 'drift': [...]}:

      ok         the box accepted the write and echoed back what was sent
      sent       a PUT actually went out (False = refused before sending)
      confirmed  a read-back after SETTLE seconds shows it stored
      drift      other items of the object that differ after the write from
                 before it — there should be none

    The session must be one that has just read from this box (read()), so the
    PUT carries its Digest login on the first try."""
    out = {"ok": False, "sent": False, "confirmed": False, "detail": "", "drift": []}
    r = s.get(f"{url}/obix/{obj}", timeout=TIMEOUT)
    if r.status_code != 200:
        out["detail"] = f"the box answered HTTP {r.status_code} when read"
        return out
    before = parse(r.text)
    if before["root"] != "obj":
        out["detail"] = f"the box did not return its settings: {before['err'] or 'unexpected reply'}"
        return out
    why = sane(obj, before["items"])
    if why:
        out["detail"] = f"not written: {why}. The box may have just restarted; try again in a few minutes"
        return out
    for item, new in changes.items():
        it = before["items"].get(item)
        if it is None or not it["writable"]:
            out["detail"] = f"not written: the box does not offer {item} for writing"
            return out
        if _int(it["val"]) != expect.get(item):
            out["detail"] = f"not written: {item} is {it['val']} at the box now, not {expect.get(item)}. Look again before changing it"
            return out
        if (it["min"] is not None and new < it["min"]) or (it["max"] is not None and new > it["max"]):
            out["detail"] = f"not written: the box allows {it['min']} to {it['max']} for {item}"
            return out
    sleep(1)
    r = s.put(f"{url}/obix/{obj}", data=body(changes), headers={"Content-Type": "text/xml"}, timeout=TIMEOUT)
    out["sent"] = True
    if r.status_code == 401:
        out["detail"] = "the box refused the login for writing (it needs a Manager or Admin login)"
        return out
    if r.status_code != 200:
        out["detail"] = f"the box answered HTTP {r.status_code} to the write"
        return out
    reply = parse(r.text)
    if reply["root"] != "obj":
        out["detail"] = f"the box refused the write: {reply['err'] or 'unexpected reply'}"
        return out
    wrong = [i for i, v in changes.items() if _int((reply["items"].get(i) or {}).get("val")) != v]
    if wrong:
        out["detail"] = (f"the box answered with a different value for {wrong[0]} "
                         f"({(reply['items'].get(wrong[0]) or {}).get('val')} instead of {changes[wrong[0]]})")
        return out
    out["ok"] = True
    sleep(SETTLE)
    try:
        r = s.get(f"{url}/obix/{obj}", timeout=TIMEOUT)
        after = parse(r.text) if r.status_code == 200 else {"root": None, "items": {}}
    except requests.RequestException:
        after = {"root": None, "items": {}}
    if after["root"] == "obj":
        out["confirmed"] = all(_int((after["items"].get(i) or {}).get("val")) == v for i, v in changes.items())
        out["drift"] = sorted(n for n, it in before["items"].items()
                              if n not in changes and n not in SKIP and n in after["items"]
                              and after["items"][n]["val"] != it["val"])
    out["detail"] = "the box accepted it" + (" and a read-back shows it stored" if out["confirmed"]
                                             else ", but a read-back did not show it stored yet")
    return out
