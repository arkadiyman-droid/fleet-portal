"""
tests/test_legacy_settings_db.py — changing a 2010 oBIX box's settings from
the portal (the box-facing half first, no database; then the page)

The fake box below answers the way the real gateway does, as read from its
source: a PUT of bare items by name, a 200 with the whole object or with an
<err>, silent clamping, a clock stamp that is not writable.
"""
import datetime as dt
import re

import pytest

from app import box_settings as bx, legacy_settings as ls
from tests.conftest import requires_postgres
from tests.test_viewer_access_db import pat, root, world      # noqa: F401  (fixtures)

REASON = "Owner asked for a later night"
EMS = {"dayStartHour": (6, 0, 23), "nightStartHour": (22, 0, 23), "winterStartMonth": (9, 1, 12),
       "winterStartDay": (1, 1, 31), "summerStartMonth": (5, 1, 12), "summerStartDay": (15, 1, 31),
       "outsideDaySetpoint": (55, 10, 70), "outsideNightSetpoint": (50, 10, 70), "ditMode": (1, 0, 4),
       "insideDaySetpoint": (72, 50, 90), "insideNightSetpoint": (71, 50, 90), "mixHighTempSetpoint": (140, 0, 255)}


def xml(obj, values, extra=""):
    lines = [f'<?xml version="1.0" encoding="UTF-8"?>\n<obj href="http://box:8060/obix/{obj}" is="useg:X" writable="true" >',
             "<!--USE-Group Object -->", '<abstime name="serverTime" val="2026-11-21T02:16:20Z"/>']
    lines += [f'<int name="{n}" val="{v}" writable="true" min="{lo}" max="{hi}"/>' for n, (v, lo, hi) in values.items()]
    return "\n".join(lines) + extra + "\n</obj>"


def dits(**change):
    vals = {f"{stem}DITHour{h}": (71 if 5 <= h < 22 else 70, 50, 90) for stem, _g in ls.DAYS for h in range(24)}
    vals.update({k: (v, 50, 90) for k, v in change.items()})
    return vals


class Reply:
    __test__ = False

    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class Box:
    __test__ = False
    """A gateway: obix/emsSettings, obix/DITs, obix/about. `quirk` picks a
    misbehaviour to test a guard against."""

    def __init__(self, quirk=None, level="Admin", up_min=45.0):
        self.ems, self.dits, self.quirk, self.level, self.up_min = dict(EMS), dits(), quirk, level, up_min
        self.calls, self.puts = [], []

    def store(self, path):
        return self.ems if path.endswith("emsSettings") else self.dits

    def get(self, url, timeout=None):
        self.calls.append(("GET", url.rsplit("/", 1)[-1]))
        if url.endswith("/about"):
            boot = dt.datetime(2026, 11, 21, 2, 16, 20) - dt.timedelta(minutes=self.up_min)
            return Reply(200, '<?xml version="1.0"?>\n<obj href="x" is="useg:About" >\n'
                              '<abstime name="serverTime" val="2026-11-21T02:16:20Z"/>\n'
                              f'<abstime name="serverBootTime" val="{boot:%Y-%m-%dT%H:%M:%SZ}"/>\n'
                              f'<str name="accessLevel" val="{self.level}"/>\n</obj>')
        obj = url.rsplit("/", 1)[-1]
        if self.quirk == "http500":
            return Reply(500)
        return Reply(200, xml(obj, self.store(url)))

    def put(self, url, data=None, headers=None, timeout=None):
        obj = url.rsplit("/", 1)[-1]
        self.calls.append(("PUT", obj))
        self.puts.append((obj, data.decode(), dict(headers or {})))
        if self.quirk == "viewer":
            return Reply(401)
        if self.quirk == "err":
            return Reply(200, '<?xml version="1.0"?>\n<err href="x" is="obix:PermissionErr" display="item is not writable"/>')
        store = self.store(url)
        for name, val in re.findall(r'<int name="(\w+)" val="(\d+)"/>', data.decode()):
            _v, lo, hi = store[name]
            new = max(lo, min(hi, int(val)))                  # the gateway clamps, silently
            if self.quirk == "clamp":
                new = min(new, 60)
            if self.quirk != "forgets":
                store[name] = (new, lo, hi)
            if self.quirk == "drift" and "mixHighTempSetpoint" in store:
                store["mixHighTempSetpoint"] = (0, 0, 255)
        reply = dict(store)
        if self.quirk == "forgets":                           # says yes, then does not keep it
            for name, val in re.findall(r'<int name="(\w+)" val="(\d+)"/>', data.decode()):
                reply[name] = (int(val), store[name][1], store[name][2])
        return Reply(200, xml(obj, reply))


NOSLEEP = lambda s: None      # noqa: E731


# ── the box-facing half (pure) ───────────────────────────────────────────────

def test_a_reply_is_read_item_by_item_with_the_boxs_own_ranges():
    p = ls.parse(xml("emsSettings", EMS))
    assert p["root"] == "obj" and p["err"] is None
    assert p["items"]["insideDaySetpoint"] == {"val": "72", "writable": True, "min": 50, "max": 90}
    assert p["items"]["serverTime"]["writable"] is False and "obj" not in p["items"]
    e = ls.parse('<?xml version="1.0"?>\n<err href="x" is="obix:BadUriErr" display="item name not found: nope"/>')
    assert e["root"] == "err" and e["err"] == "item name not found: nope" and e["items"] == {}
    assert ls.parse("")["root"] is None and ls.parse(None)["items"] == {}


def test_only_what_the_box_lists_as_writable_is_offered():
    objects = {"emsSettings": ls.parse(xml("emsSettings", EMS)), "DITs": ls.parse(xml("DITs", dits()))}
    fields = ls.fields(objects)
    by = {f["key"]: f for f in fields}
    assert len(fields) == 10 + 72
    cut = by["emsSettings.outsideDaySetpoint"]
    assert (cut["label"], cut["kind"], cut["lo"], cut["hi"], cut["value"], cut["group"]) == (
        "Outdoor cutoff, day", "deg", 10, 70, 55, "Outdoor cutoff")
    assert by["emsSettings.insideDaySetpoint"]["label"] == "Heat-off point, day"      # not "setpoint": it is not the target
    hour = by["DITs.weekdayDITHour5"]
    assert (hour["label"], hour["cell"], hour["value"], hour["lo"], hour["hi"]) == ("Weekdays target, 5 AM", "5a", 71, 50, 90)
    assert by["DITs.sundayDITHour22"]["cell"] == "10p" and "emsSettings.ditMode" not in by
    locked = xml("emsSettings", EMS).replace('name="outsideDaySetpoint" val="55" writable="true"', 'name="outsideDaySetpoint" val="55"')
    assert "emsSettings.outsideDaySetpoint" not in {f["key"] for f in ls.fields({"emsSettings": ls.parse(locked)})}
    assert ls.fields({}) == []


def test_a_box_whose_settings_read_back_as_nonsense_is_not_written_to():
    good = ls.parse(xml("emsSettings", EMS))["items"]
    assert ls.sane("emsSettings", good) is None and ls.sane("DITs", ls.parse(xml("DITs", dits()))["items"]) is None
    zeros = ls.parse(xml("DITs", {k: (0, 50, 90) for k in dits()}))["items"]      # the gateway's copy is empty
    assert "out of any sensible range" in ls.sane("DITs", zeros)
    blank = ls.parse(xml("emsSettings", {**EMS, "insideDaySetpoint": (0, 50, 90), "dayStartHour": (255, 0, 23)}))["items"]
    assert "2 of its values" in ls.sane("emsSettings", blank)
    assert "did not list" in ls.sane("emsSettings", {})


def test_the_body_is_only_the_changed_items_and_nothing_that_could_be_misread():
    assert ls.body({"outsideDaySetpoint": 56, "nightStartHour": 23}) == \
        b'<int name="outsideDaySetpoint" val="56"/>\r\n<int name="nightStartHour" val="23"/>\r\n'
    for item, v in (("outsideDaySetpoint", -1), ("outsideDaySetpoint", 56.5), ("outsideDaySetpoint", "56"),
                    ("outsideDaySetpoint", True), ('x" val="1"/><obj href="obix/emsConfig', 1), ("", 1)):
        with pytest.raises(ValueError):
            ls.body({item: v})


def test_a_write_reads_first_sends_only_the_change_and_reads_back():
    box = Box()
    o = ls.write(box, "http://box:8060", "emsSettings", {"outsideDaySetpoint": 56}, {"outsideDaySetpoint": 55}, sleep=NOSLEEP)
    assert (o["ok"], o["sent"], o["confirmed"], o["drift"]) == (True, True, True, [])
    assert box.calls == [("GET", "emsSettings"), ("PUT", "emsSettings"), ("GET", "emsSettings")]
    obj, body, headers = box.puts[0]
    assert body == '<int name="outsideDaySetpoint" val="56"/>\r\n' and headers == {"Content-Type": "text/xml"}
    assert "<obj" not in body and "serverTime" not in body and box.ems["outsideDaySetpoint"][0] == 56


def test_every_way_a_write_can_go_wrong_is_caught_and_said():
    def go(quirk=None, changes=None, expect=None, prep=None):
        box = Box(quirk)
        if prep:
            prep(box)
        return box, ls.write(box, "http://box", "emsSettings", changes or {"outsideDaySetpoint": 56},
                             expect or {"outsideDaySetpoint": 55}, sleep=NOSLEEP)

    box, o = go(expect={"outsideDaySetpoint": 54})            # it moved at the box since the person was shown it
    assert not o["sent"] and "is 55 at the box now, not 54" in o["detail"] and box.puts == []
    box, o = go(changes={"outsideDaySetpoint": 75})           # outside the box's own range: never sent to be clamped
    assert not o["sent"] and "allows 10 to 70" in o["detail"] and box.puts == []
    box, o = go(changes={"noSuchItem": 1}, expect={"noSuchItem": 0})
    assert not o["sent"] and "does not offer" in o["detail"]
    box, o = go(prep=lambda b: b.ems.update(insideDaySetpoint=(0, 50, 90), insideNightSetpoint=(0, 50, 90)))
    assert not o["sent"] and "not written" in o["detail"] and "sensible range" in o["detail"] and box.puts == []
    box, o = go("http500")
    assert not o["sent"] and "HTTP 500" in o["detail"]
    box, o = go("viewer")
    assert o["sent"] and not o["ok"] and "Manager or Admin" in o["detail"]
    box, o = go("err")
    assert o["sent"] and not o["ok"] and "item is not writable" in o["detail"]
    box, o = go("clamp", changes={"outsideDaySetpoint": 65})  # the box answered with another value than was sent
    assert not o["ok"] and "a different value" in o["detail"] and "60 instead of 65" in o["detail"]
    box, o = go("forgets")
    assert o["ok"] and not o["confirmed"] and "did not show it stored" in o["detail"]
    box, o = go("drift")                                      # something else moved during the write
    assert o["ok"] and o["confirmed"] and o["drift"] == ["mixHighTempSetpoint"]


def test_each_request_closes_its_connection_and_a_dropped_read_is_tried_once_more():
    import requests
    assert ls.session("u", "p").headers["Connection"] == "close"

    class Drops(Box):
        __test__ = False

        def __init__(self, drops):
            super().__init__()
            self.drops = drops

        def get(self, url, timeout=None):
            if self.drops:
                self.drops -= 1
                raise requests.ConnectionError("Remote end closed connection without response")
            return super().get(url, timeout)

        def put(self, url, data=None, headers=None, timeout=None):
            raise requests.ConnectionError("dropped")

    assert ls.read(Drops(1), "http://box", sleep=NOSLEEP)["emsSettings"]["root"] == "obj"
    with pytest.raises(requests.ConnectionError):
        ls.read(Drops(2), "http://box", sleep=NOSLEEP)              # twice in a row: give up, say so
    box = Drops(0)
    with pytest.raises(requests.ConnectionError):                   # a write is never retried
        ls.write(box, "http://box", "emsSettings", {"outsideDaySetpoint": 56}, {"outsideDaySetpoint": 55}, sleep=NOSLEEP)
    assert box.calls == [("GET", "emsSettings")]


def test_the_login_level_and_how_long_the_box_has_been_up():
    assert ls.about(Box(level="Admin", up_min=45), "http://box") == {"level": "Admin", "up_min": 45.0}
    assert ls.about(Box(level="User", up_min=1.5), "http://box") == {"level": "User", "up_min": 1.5}


# ── the page ─────────────────────────────────────────────────────────────────

@pytest.fixture
def gateway(monkeypatch, world, session):
    """The world's Berry box, faked. Returns the Box."""
    import time
    box = Box()
    monkeypatch.setattr(ls, "session", lambda user, password: box)
    monkeypatch.setattr(time, "sleep", NOSLEEP)
    monkeypatch.setattr(ls.time, "sleep", NOSLEEP)
    monkeypatch.setattr(ls, "read", lambda s, url, objs=("emsSettings", "DITs"), pause=2.0, sleep=NOSLEEP:
                        {o: ls.parse(s.get(f"{url}/obix/{o}").text) for o in objs})
    real_write = ls.write
    monkeypatch.setattr(ls, "write", lambda s, url, obj, changes, expect, sleep=NOSLEEP: real_write(s, url, obj, changes, expect, sleep=NOSLEEP))
    return box


def _form(box, **over):
    data = {f"emsSettings.{k}": str(v[0]) for k, v in box.ems.items() if k != "ditMode" and k != "mixHighTempSetpoint"}
    data.update({f"DITs.{k}": str(v[0]) for k, v in box.dits.items()})
    data.update(over)
    return data


def _confirm(page, **over):
    card = page[page.index("What will change at"):page.index('name="step" value="preview"')]
    data = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', card))
    data["note"] = REASON
    data.update(over)
    return data


@requires_postgres
def test_the_page_shows_the_boxs_settings_and_sends_nothing(root, world, gateway):
    page = root.get(f"/live/{world['box_mine']}/settings").get_data(as_text=True)
    for text in ("Outdoor cutoff, day", "Heat-off point, night", "Indoor target by hour, weekdays",
                 "Indoor target by hour, Sunday", "Outdoor adjustment is on at this box."):
        assert text in page, text
    assert 'name="emsSettings.outsideDaySetpoint" value="55" min="10" max="70"' in page
    assert 'name="DITs.weekdayDITHour5" value="71" min="50" max="90"' in page and page.count('class="st-cell') == 72
    assert "Review changes" in page and gateway.puts == []
    assert "Change settings…" in root.get(f"/live/{world['box_mine']}").get_data(as_text=True)


@requires_postgres
def test_a_confirmed_change_is_written_read_back_and_recorded(root, world, gateway, session):
    import app.models as m
    from app import setting_audit
    url = f"/live/{world['box_mine']}/settings"
    setting_audit.track("legacy", world["box_mine"], None, "50 South 9 St",
                        {"emsSettings.nightStartHour": "22", "DITs.weekdayDITHour22": "70"}, setting_audit.legacy_describe)
    session.commit()
    preview = root.post(url, data={**_form(gateway, **{"emsSettings.nightStartHour": "23", "DITs.weekdayDITHour22": "71"}),
                                   "step": "preview"}).get_data(as_text=True)
    assert "Send 2 changes to 50 South 9 St" in preview and gateway.puts == []
    assert re.search(r"<td>Night period starts</td><td>10 PM</td><td>→</td><td class=\"new\">11 PM</td>", preview)
    page = root.post(url, data=_confirm(preview)).get_data(as_text=True)
    assert [(o, b) for o, b, _h in gateway.puts] == [("emsSettings", '<int name="nightStartHour" val="23"/>\r\n'),
                                                    ("DITs", '<int name="weekdayDITHour22" val="71"/>\r\n')]
    assert "Changed, and confirmed." in page and gateway.ems["nightStartHour"][0] == 23
    rows = m.SettingChange.query.order_by(m.SettingChange.id).all()
    assert sorted((r.key, r.old_text, r.new_text, r.origin, r.by, r.note) for r in rows) == [
        ("DITs.weekdayDITHour22", "70°", "71°", "portal", "the administrator", REASON),
        ("emsSettings.nightStartHour", "10 PM", "11 PM", "portal", "the administrator", REASON)]
    assert all(r.source == "legacy" and r.seen_at is not None for r in rows)
    # the hourly read that follows finds nothing new: the stored snapshot already holds the new values
    snap = session.get(m.SettingSnapshot, ("legacy", world["box_mine"]))
    session.refresh(snap)
    assert snap.data["emsSettings.nightStartHour"] == "23" and snap.data["DITs.weekdayDITHour22"] == "71"
    root.post(url, data=_confirm(preview))                    # the same confirmation again changes nothing more
    assert len(gateway.puts) == 2 and m.SettingChange.query.count() == 2


@requires_postgres
def test_a_refusal_at_the_box_changes_nothing_and_stops_there(root, world, gateway):
    import app.models as m
    url = f"/live/{world['box_mine']}/settings"
    preview = root.post(url, data={**_form(gateway, **{"emsSettings.outsideDaySetpoint": "58", "DITs.sundayDITHour3": "69"}),
                                   "step": "preview"}).get_data(as_text=True)
    gateway.quirk = "err"
    page = root.post(url, data=_confirm(preview)).get_data(as_text=True)
    assert "Not changed." in page and "item is not writable" in page
    assert [o for o, _b, _h in gateway.puts] == ["emsSettings"]       # the second object was never tried
    assert m.SettingChange.query.count() == 0
    gateway.quirk = None
    page = root.post(url, data={**_form(gateway, **{"emsSettings.dayStartHour": "23"}), "step": "send", "digest": "x",
                                "note": REASON}).get_data(as_text=True)
    assert "Nothing can be sent yet." in page and "the day has to start before the night" in page and len(gateway.puts) == 1


@requires_postgres
def test_a_box_that_must_not_be_written_to_is_read_only(root, world, gateway):
    url = f"/live/{world['box_mine']}/settings"
    gateway.level = "User"
    page = root.get(url).get_data(as_text=True)
    assert "may only read" in page and "Review changes" not in page
    gateway.level, gateway.up_min = "Admin", 1.0
    assert "started a moment ago" in root.get(url).get_data(as_text=True)
    gateway.up_min = 45.0
    gateway.dits = {k: (0, 50, 90) for k in gateway.dits}
    page = root.get(url).get_data(as_text=True)
    assert "do not read back sensibly" in page and "Review changes" not in page
    root.post(url, data={"emsSettings.outsideDaySetpoint": "58", "step": "send", "digest": "x", "note": REASON})
    assert gateway.puts == []


@requires_postgres
def test_only_the_administrator_or_a_ticked_person_gets_the_page(root, pat, world, gateway):
    import app.models as m
    url = f"/live/{world['box_mine']}/settings"
    assert pat.get(url).status_code == 403 and gateway.calls == []
    assert "Change settings" not in pat.get(f"/live/{world['box_mine']}").get_data(as_text=True)
    root.post(f"/users/{world['viewer']}/properties",
              data={"property_id": [world["berry"]], "set_offered": [world["berry"]], "set_property_id": [world["berry"]]})
    assert pat.get(f"/live/{world['box_dca']}/settings").status_code == 403       # another property
    preview = pat.post(url, data={**_form(gateway, **{"emsSettings.outsideNightSetpoint": "48"}), "step": "preview"}
                       ).get_data(as_text=True)
    pat.post(url, data=_confirm(preview))
    r = m.SettingChange.query.one()
    assert (r.by, r.label, r.old_text, r.new_text) == ("Pat Viewer", "Outdoor cutoff, night", "50°", "48°")
