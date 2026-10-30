"""
app/rc_dhw.py — Reliable Controls MACH-ProWeb DHW, read view-only

A fourth monitored source beside the EchoTherm controllers
(app/fleet_poller.py), the 2010 oBIX boxes (app/legacy.py) and Bloom
(app/bloom.py). Added to pull the Whitney Young / Nepperhan **DHW System A/B**
data, which is NOT in the Bloom feed (Empowered Buildings only ingested the
dual-temp heating/cooling side). The underlying BAS is a Reliable Controls
MACH-ProWeb controller at http://162.184.200.208:47002/.

Why a headless browser, not an HTTP/BACnet poller (confirmed 2026-10-30):
  * The controller serves live values only through RC's proprietary
    `confirmedPrivateTransfer` (BACnet service 18) bulk blob over POST
    /BACnet/query. It is NOT exposed as standard BACnet AnalogInput objects
    (a ReadProperty of AnalogInput 3 on device 200000 returns a BACnet Error
    PDU), and the blob is an undocumented vendor format.
  * BACnet/IP (UDP 47808) is not reachable — only TCP :47002 is forwarded, and
    a public BACnet forward would be unauthenticated read+write.
  * But the controller's own JavaScript already decodes every value into the
    page DOM: each live value is a `<div class="rcPoint" data-gel-id="gel_NNN">`
    with a STABLE element id. So we run that JavaScript in a headless Chromium
    (already installed on beverly-pi, /usr/bin/chromium) and read the rendered
    values by their gel id. No protocol replication, no site change.

Read-only guarantee: this module only READS DOM text. It never clicks an
editable point, submits an override, or writes to the controller.

Poll model mirrors app/bloom.py: a daemon thread per process, one Postgres
advisory lock per cycle, log-and-swallow throughout. One Chromium is launched
per cycle (login once, read every tower, close) so steady-state memory on the
Pi stays low. The full gel map is stored as raw JSON (RcDhwStatus.data); the
promoted numbers are derived from the confirmed gel ids below.

Gel map (identical on the Tower A and Tower B graphics — one shared template):
  gel_208  DHW Supply To Building   (°F)
  gel_152  DHW Return From Building (°F)
  gel_310  Pump Running             (Yes/No)
  gel_320/324/328/332  DHW HWP alarm cells (Normal / alarm text)
Everything else on the page is kept in `data` for later promotion.
"""
import base64
import datetime as _dt
import logging
import re
import threading
import time

from sqlalchemy import text

from app.extensions import db

log = logging.getLogger(__name__)

_LOCK_KEY = 875_219_004            # 001 fleet, 002 legacy/billing, 003 bloom, 004 = this

# Confirmed gel ids (both Tower A and Tower B share the graphic template).
GEL_SUPPLY = "gel_208"
GEL_RETURN = "gel_152"
GEL_PUMP_RUNNING = "gel_310"
GEL_ALARMS = ("gel_320", "gel_324", "gel_328", "gel_332")


# ── value parsing ────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _num(txt):
    """First number in e.g. '124.0 °F' / '100.0 %' / '8.5 Hours' -> float, or None."""
    if not txt:
        return None
    m = _NUM_RE.search(txt)
    return float(m.group()) if m else None


def _is_on(txt):
    if not txt:
        return None
    t = txt.strip().lower()
    if t in ("yes", "on", "running", "true"):
        return True
    if t in ("no", "off", "stopped", "false"):
        return False
    return None


def _is_alarm(txt):
    """An alarm cell reads 'Normal' when healthy; anything else (and present) is an alarm."""
    if not txt:
        return False
    return txt.strip().lower() not in ("normal", "ok", "")


def parse_gels(gels):
    """Derive the promoted fields from the raw gel map."""
    alarm_cells = [gels.get(g) for g in GEL_ALARMS if gels.get(g) is not None]
    return {
        "supply_f": _num(gels.get(GEL_SUPPLY)),
        "return_f": _num(gels.get(GEL_RETURN)),
        "pump_running": _is_on(gels.get(GEL_PUMP_RUNNING)),
        "alarm": (any(_is_alarm(c) for c in alarm_cells) if alarm_cells else None),
    }


# ── headless scrape ──────────────────────────────────────────────────────────

# JS, run in the page, that collects every live point as {gel_id: "text"}.
_COLLECT_JS = (
    "() => { const o = {};"
    " document.querySelectorAll('.rcPoint').forEach(e => {"
    "   const g = e.dataset && e.dataset.gelId;"
    "   const t = (e.textContent || '').trim();"
    "   if (g && t) o[g] = t; });"
    " return o; }"
)


def _chromium_args(app):
    return app.config.get("RC_DHW_CHROMIUM_ARGS", [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-extensions", "--mute-audio",
    ])


def scrape(app, controller, towers):
    """Launch one headless Chromium, log into `controller`, and read each tower.
    Returns {tower_id: {"ok":bool, "gels":{...}, "parsed":{...}, "error":str}}.
    Read-only: only DOM text is read. App context not required here."""
    from playwright.sync_api import sync_playwright

    timeout_ms = int(app.config.get("RC_DHW_PAGE_TIMEOUT_SECONDS", 45)) * 1000
    settle_ms = int(app.config.get("RC_DHW_SETTLE_MS", 1500))
    exe = app.config.get("RC_DHW_CHROMIUM_PATH", "/usr/bin/chromium")
    base = controller.base_url.rstrip("/")
    out = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe, headless=True, args=_chromium_args(app))
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            _login(page, base, controller.username, controller.password, timeout_ms)
            for t in towers:
                try:
                    gels = _read_tower(page, t.group, timeout_ms, settle_ms)
                    out[t.id] = {"ok": True, "gels": gels, "parsed": parse_gels(gels), "error": None}
                except Exception as e:
                    log.warning("RC DHW scrape failed for tower %s (%s): %s", t.id, t.group, e)
                    out[t.id] = {"ok": False, "gels": {}, "parsed": {}, "error": str(e)[:300]}
        finally:
            browser.close()
    return out


def _login(page, base, user, pw, timeout_ms):
    page.goto(base + "/", wait_until="domcontentloaded", timeout=timeout_ms)
    # The MACH-ProWeb login form (Dojo): #username / #password / button.logonButton.
    try:
        page.wait_for_selector("#username", timeout=timeout_ms)
        page.fill("#username", user)
        page.fill("#password", pw)
    except Exception:
        page.fill("input[type=text]", user)
        page.fill("input[type=password]", pw)
    try:
        page.click("button.logonButton", timeout=5000)
    except Exception:
        page.press("#password", "Enter")
    # Logged in once the app has rendered live points.
    page.wait_for_function("document.querySelectorAll('.rcPoint').length > 0", timeout=timeout_ms)


def _read_tower(page, group, timeout_ms, settle_ms):
    """Switch the SPA to the tower's graphic (URL hash = base64(group)) and read it."""
    page_hash = "#" + base64.b64encode(group.encode()).decode()
    page.evaluate("(h) => { location.hash = h; }", page_hash)
    # Wait for the view to actually be this group (title is e.g. "200000GRP2 | ..."),
    # so we never read the previous tower's stale nodes...
    page.wait_for_function("(g) => document.title.indexOf(g) === 0", arg=group, timeout=timeout_ms)
    # ...then wait until the supply point has a value, then let the rest settle.
    page.wait_for_function(
        "() => { const e = document.querySelector('[data-gel-id=\"gel_208\"]');"
        " return e && /[0-9]/.test(e.textContent); }",
        timeout=timeout_ms,
    )
    page.wait_for_timeout(settle_ms)
    return page.evaluate(_COLLECT_JS)


# ── poller ───────────────────────────────────────────────────────────────────

class RcDhwPoller:
    def __init__(self):
        self._thread = None

    def start(self, app):
        if not app.config.get("RC_DHW_POLLER_ENABLED", True):
            log.info("RC DHW poller disabled (RC_DHW_POLLER_ENABLED=0)")
            return
        self._thread = threading.Thread(target=self._run, args=(app,), name="rc-dhw-poller", daemon=True)
        self._thread.start()
        log.info("RC DHW poller started")

    def _run(self, app):
        interval = app.config.get("RC_DHW_POLL_INTERVAL_SECONDS", 300)
        while True:
            try:
                self._run_cycle(app)
            except Exception as e:
                log.warning("RC DHW poller cycle failed: %s", e)
            time.sleep(interval)

    def _run_cycle(self, app):
        from app.models import RcController
        with app.app_context():
            conn = db.engine.connect()
            try:
                if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar():
                    return
                for ctrl in RcController.query.filter(RcController.is_active.is_(True)).all():
                    try:
                        poll_controller(app, ctrl)
                    except Exception as e:
                        db.session.rollback()
                        log.warning("RC DHW poll failed for controller %s (%s): %s", ctrl.id, ctrl.label, e)
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
                conn.close()


def poll_controller(app, controller):
    """One headless login + read of every active tower on `controller`, then
    upsert an RcDhwStatus per tower. App context assumed."""
    from app.models import RcDhwTower, RcDhwStatus
    towers = (RcDhwTower.query
              .filter(RcDhwTower.controller_id == controller.id, RcDhwTower.is_active.is_(True))
              .all())
    if not towers:
        log.info("RC DHW %s: no active towers", controller.label)
        return

    started = time.monotonic()
    results = scrape(app, controller, towers)
    ms = int((time.monotonic() - started) * 1000)
    now = _dt.datetime.utcnow()

    seen = 0
    for t in towers:
        r = results.get(t.id) or {"ok": False, "gels": {}, "parsed": {}, "error": "not scraped"}
        st = db.session.get(RcDhwStatus, t.id) or RcDhwStatus(tower_id=t.id)
        st.polled_at = now
        st.response_ms = ms
        if r["ok"]:
            p = r["parsed"]
            st.ok = True
            st.error = None
            st.last_ok_at = now
            st.supply_f = p.get("supply_f")
            st.return_f = p.get("return_f")
            st.pump_running = p.get("pump_running")
            st.alarm = p.get("alarm")
            st.data = r["gels"]
            seen += 1
        else:
            st.ok = False
            st.error = r.get("error")
        db.session.add(st)
    db.session.commit()
    log.info("RC DHW poll %s: %d/%d towers read in %dms", controller.label, seen, len(towers), ms)


rc_dhw_poller = RcDhwPoller()
