"""
app/box_settings.py — a box's settings, changed from the portal: the part every kind shares

A "box" here is anything the portal can ask to change a setting: an EchoTherm
controller (routes.fleet_controller_settings) or a 2010 oBIX box
(routes.live_box_settings). Each kind brings its own list of settings — read
LIVE from the box, with the box's own ranges — and its own way of sending.
What they share is this module, which is pure:

  field   {key, label, kind, lo, hi, value, help, group}
          kind: deg (whole degrees F) / hour (0-23) / month / day / num
  parse() what a person typed -> {key: number}
  plan()  what would change, old -> new, with every refusal said on its own
          row, and NOTHING to send if any row is refused (all or nothing)
  checks  rules across settings (the day starts before the night; a date
          exists), given to plan() by the kind that needs them

The Bloom buildings' page (app/bloom_settings.py) predates this and keeps its
own module: Bloom's values are encoded its own way and are sent as point
objects, not as key/value pairs.
"""
import datetime as _dt
import hashlib
import json

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def _num(v):
    if v is None or isinstance(v, (bool, dict, list)):
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def hour_words(h) -> str:
    h = int(h)
    return f"{(h % 12) or 12} {'AM' if h % 24 < 12 else 'PM'}"


def text(kind, value) -> str:
    """A stored value as a person reads it."""
    n = _num(value)
    if n is None:
        return "—"
    if kind == "deg":
        return f"{n:g}°"
    if kind == "hour" and n == int(n) and 0 <= n <= 24:
        return hour_words(n)
    if kind == "month" and n == int(n) and 1 <= n <= 12:
        return MONTHS[int(n) - 1]
    return f"{n:g}"


def parse(form, fields) -> tuple:
    """(wanted, errors) from a submitted form. A field left out or blank is
    simply not in `wanted` (= leave that setting alone)."""
    wanted, errors = {}, {}
    for f in fields:
        v = (form.get(f["key"]) or "").strip()
        if not v:
            continue
        n = _num(v)
        if n is None:
            errors[f["key"]] = "not a number"
        else:
            wanted[f["key"]] = int(n) if n == int(n) else n
    return wanted, errors


# ── rules across settings ────────────────────────────────────────────────────

def before(first, second, why):
    """The `first` setting has to be less than the `second` one."""
    def check(after):
        a, b = _num(after.get(first)), _num(after.get(second))
        return {first: why, second: why} if a is not None and b is not None and a >= b else {}
    return check


def real_date(month_key, day_key):
    """A month and a day that make a date that exists every year."""
    def check(after):
        m, d = _num(after.get(month_key)), _num(after.get(day_key))
        if m is None or d is None:
            return {}
        try:
            _dt.date(2001, int(m), int(d))
            return {}
        except ValueError:
            return {month_key: "that date does not exist", day_key: "that date does not exist"}
    return check


# ── the change ───────────────────────────────────────────────────────────────

def plan(fields, wanted=None, form_errors=None, checks=()) -> dict:
    """What setting this box to `wanted` would change. Returns rows (one per
    field, in order), `changes` {key: new} and `expect` {key: old} for the
    send — both EMPTY when any row is refused — a `digest` of them, and a
    `log` of the changes in the ledger's shape."""
    wanted, form_errors = dict(wanted or {}), form_errors or {}
    rows = []
    for f in fields:
        key, old = f["key"], f.get("value")
        row = {**f, "old": old, "old_text": text(f["kind"], old), "new": None, "new_text": None, "change": False,
               "error": form_errors.get(key), "note": None}
        if key in wanted and not row["error"]:
            new = wanted[key]
            if _num(old) is not None and _num(old) == float(new):
                pass                                   # already that value: nothing to send
            else:
                row["new"], row["new_text"], row["change"] = new, text(f["kind"], new), True
                unit = "°" if f["kind"] == "deg" else ""
                if new != int(new):
                    row["error"] = "whole numbers only"
                elif f.get("lo") is not None and not f["lo"] <= new <= f["hi"]:
                    row["error"] = f"allowed {f['lo']}{unit} to {f['hi']}{unit}"
        rows.append(row)
    by = {r["key"]: r for r in rows}
    after = {r["key"]: (r["new"] if r["change"] and not r["error"] else r["old"]) for r in rows}
    for check in checks:
        for key, why in check(after).items():
            r = by.get(key)
            if r is not None and r["change"] and not r["error"]:
                r["error"] = why
    errors = any(r["error"] for r in rows)
    changed = [] if errors else [r for r in rows if r["change"]]
    changes = {r["key"]: r["new"] for r in changed}
    expect = {r["key"]: r["old"] for r in changed}
    digest = hashlib.sha256(json.dumps(sorted((k, changes[k], expect[k]) for k in changes), default=str)
                            .encode()).hexdigest()[:16] if changes else ""
    return {"rows": rows, "changes": changes, "expect": expect, "digest": digest, "n_changes": len(changes),
            "errors": errors,
            "log": [{"key": r["key"], "label": r["label"], "old": r["old"], "new": r["new"],
                     "old_text": r["old_text"], "new_text": r["new_text"]} for r in changed]}


def landed(fields_now, wanted) -> bool:
    """True when the box now holds every value that was asked for."""
    now = {f["key"]: f.get("value") for f in fields_now}
    return all(_num(now.get(k)) is not None and _num(now.get(k)) == float(v) for k, v in wanted.items())


def form_groups(p: dict) -> list:
    """A plan's rows laid out for the form, group by group; each row gains
    `field`, the value its input shows (the one asked for, else the box's)."""
    out, order = {}, []
    for r in p["rows"]:
        r["field"] = r["new"] if r["change"] else r["old"]
        g = r.get("group") or "Settings"
        if g not in out:
            out[g] = []
            order.append(g)
        out[g].append(r)
    return [{"name": g, "rows": out[g]} for g in order]


def hidden_fields(wanted) -> list:
    """`wanted` as (field name, value) pairs: the hidden inputs that carry a
    previewed change to its confirmation, readable by parse() again."""
    return [(k, f"{v:g}" if isinstance(v, float) else v) for k, v in wanted.items()]


def summary(log) -> str:
    return "; ".join(f"{c['label']} {c['old_text']} → {c['new_text']}" for c in log or [])
