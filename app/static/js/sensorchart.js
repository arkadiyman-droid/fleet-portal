/*
 * app/static/js/sensorchart.js — the "click a temperature" chart window
 * (Live page and a box's page). Needs charts.js first.
 *
 * Any element with class "lv-t" and data-k="<sensor key>[,<key>...]" inside an element
 * with data-box="<2010 box id>" or data-ctrl="<controller id>" opens the window: that sensor as a line, chips to
 * add the box's other sensors, and the burner's heat / hot-water minutes per
 * hour as stacked bars under it on the same time axis. Data comes from
 * /live/<id>/series.json (app/analytics.box_sensor_series) or, for a
 * controller, /fleet/<id>/series.json (app/ctrl_history.series).
 *
 * window.SensorChart.isOpen() lets a page hold its auto-reload while open.
 */
(function () {
  "use strict";
  const SERIES = {box: "/live/__ID__/series.json", ctrl: "/fleet/__ID__/series.json"};
  const COLORS = ["#2563eb", "#16a34a", "#ea580c", "#dc2626", "#9333ea", "#0891b2", "#ca8a04", "#db2777",
                  "#4b5563", "#65a30d", "#7c3aed", "#0d9488"];
  const RANGES = [["1", "24 h"], ["2", "2 days"], ["7", "7 days"], ["30", "30 days"]];

  const css = document.createElement("style");
  css.textContent = `
  .lv-t { cursor:pointer; } .lv-t:hover { outline:2px solid #93c5fd; outline-offset:-2px; border-radius:4px; }
  .lvx { position:fixed; inset:0; z-index:50; background:rgba(15,23,42,.45); display:flex; align-items:flex-start; justify-content:center; padding:4vh 2vw; }
  .lvx[hidden] { display:none; }
  .lvx-box { background:#fff; border-radius:12px; width:min(1500px, 96vw); max-height:92vh; overflow:auto; padding:14px 18px 16px; box-shadow:0 20px 50px rgba(0,0,0,.25); }
  .lvx-head { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:6px; }
  .lvx-head h2 { margin:0 8px 0 0; font-size:17px; }
  .lvx-head button.r { padding:3px 10px; border:1px solid var(--border); border-radius:14px; background:#fff; cursor:pointer; font:inherit; font-size:13px; color:var(--muted); }
  .lvx-head button.r.on { background:var(--text); color:#fff; border-color:var(--text); }
  .lvx-x { margin-left:auto; border:none; background:none; font-size:26px; line-height:1; cursor:pointer; color:var(--muted); padding:0 4px; }
  .lvx-sub { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; font-weight:600; margin:10px 0 0; }
  .lvx-add { margin-top:12px; border-top:1px solid var(--border); padding-top:10px; }
  .lvx-grp { display:flex; flex-wrap:wrap; gap:5px; align-items:center; margin:4px 0; }
  .lvx-grp b { font-size:12px; color:var(--muted); min-width:96px; }
  .lvx-chip { border:1px solid var(--border); background:#fff; border-radius:14px; padding:2px 10px; font:inherit; font-size:12.5px; cursor:pointer; }
  .lvx-chip.on { color:#fff; border-color:transparent; }
  .lvx-note { font-size:12px; color:#b45309; margin:6px 0 0; }
  .lv-chartbtn { display:inline-block; font-size:11px; font-weight:600; padding:0 7px; border:1px solid #bfdbfe; border-radius:10px; color:#1d4ed8; background:#eff6ff; margin-left:6px; vertical-align:1px; }`;
  document.head.appendChild(css);

  const dlg = document.createElement("div");
  dlg.className = "lvx"; dlg.hidden = true;
  dlg.setAttribute("role", "dialog"); dlg.setAttribute("aria-modal", "true"); dlg.setAttribute("aria-labelledby", "lvx-title");
  dlg.innerHTML = '<div class="lvx-box"><div class="lvx-head"><h2 id="lvx-title"></h2>' +
    RANGES.map(([k, l]) => `<button class="r" type="button" data-days="${k}">${l}</button>`).join("") +
    '<a id="lvx-open" class="card-sub" href="#" style="margin-left:6px">full history</a>' +
    '<span class="card-sub" id="lvx-res"></span>' +
    '<button class="lvx-x" type="button" aria-label="Close" id="lvx-close">&times;</button></div>' +
    '<div id="lvx-chart"></div><div class="lvx-sub">Burner run time, minutes per hour</div><div id="lvx-bars"></div>' +
    '<p class="lvx-note" id="lvx-note" hidden></p><div class="lvx-add" id="lvx-add"></div></div>';
  document.body.appendChild(dlg);
  const $ = id => document.getElementById(id);
  let cur = null;                            // {box, keys, days, catalog}

  function open(box, key, kind) {
    let days = "1";
    try { days = localStorage.getItem("lvx-days") || "1"; } catch (e) {}
    const keys = String(key).split(",").map(k => k.trim()).filter(Boolean);
    cur = {box: String(box), keys, days, catalog: null, url: SERIES[kind || "box"]};
    dlg.hidden = false;
    document.body.style.overflow = "hidden";
    load();
  }
  function close() { dlg.hidden = true; document.body.style.overflow = ""; cur = null; }

  document.addEventListener("click", ev => {
    const el = ev.target.closest(".lv-t");
    if (!el || !el.dataset.k) return;
    const host = el.closest("[data-box], [data-ctrl]");
    if (host) open(host.dataset.box || host.dataset.ctrl, el.dataset.k, host.dataset.box ? "box" : "ctrl");
  });
  $("lvx-close").addEventListener("click", close);
  dlg.addEventListener("click", ev => { if (ev.target === dlg) close(); });
  document.addEventListener("keydown", ev => { if (ev.key === "Escape" && !dlg.hidden) close(); });
  dlg.querySelectorAll("button.r").forEach(b => b.addEventListener("click", () => {
    cur.days = b.dataset.days;
    try { localStorage.setItem("lvx-days", cur.days); } catch (e) {}
    load();
  }));

  function load() {
    dlg.querySelectorAll("button.r").forEach(b => b.classList.toggle("on", b.dataset.days === cur.days));
    $("lvx-res").textContent = "loading…";
    const want = cur;
    fetch(cur.url.replace("__ID__", encodeURIComponent(cur.box)) + "?days=" + cur.days + "&keys=" +
          encodeURIComponent(cur.keys.join(",")), {credentials: "same-origin"}).then(r => r.json()).then(d => {
      if (cur !== want) return;
      cur.catalog = d.catalog;
      $("lvx-title").textContent = d.name;
      $("lvx-open").href = d.url + "#history";
      $("lvx-res").textContent = "Showing " + d.res + " · times in New York";
      $("lvx-note").hidden = !d.note; $("lvx-note").textContent = d.note || "";
      const color = k => COLORS[cur.keys.indexOf(k) % COLORS.length];
      EchoCharts.line($("lvx-chart"), {t: d.t, range: d.range, unit: "°", height: 300,
        series: cur.keys.map(k => ({name: d.labels[k] || k, color: color(k), data: d.series[k] || [], width: 2}))});
      const B = d.bars || {};
      EchoCharts.bars($("lvx-bars"), {t: B.t || [], range: d.range, step: "hour", unit: " min", height: 140,
        series: [{name: "Heat", color: "#f97316", data: B.heat || []}, {name: "Hot water", color: "#0ea5e9", data: B.hw || []}]});
      chips(color);
    }).catch(() => { $("lvx-res").textContent = "could not load the chart"; });
  }

  function chips(color) {
    const box = $("lvx-add");
    box.innerHTML = '<div class="card-sub" style="margin-bottom:4px">Add to the chart (click again to remove):</div>';
    const groups = {};
    (cur.catalog || []).forEach(c => (groups[c.group] = groups[c.group] || []).push(c));
    Object.entries(groups).forEach(([g, items]) => {
      const row = document.createElement("div"); row.className = "lvx-grp";
      const b = document.createElement("b"); b.textContent = g; row.appendChild(b);
      items.forEach(c => {
        const on = cur.keys.includes(c.key), chip = document.createElement("button");
        chip.type = "button"; chip.className = "lvx-chip" + (on ? " on" : ""); chip.textContent = c.label;
        if (on) chip.style.background = color(c.key);
        chip.addEventListener("click", () => {
          if (on && cur.keys.length === 1) return;          // keep at least one line
          cur.keys = on ? cur.keys.filter(k => k !== c.key) : cur.keys.concat([c.key]).slice(0, 12);
          load();
        });
        row.appendChild(chip);
      });
      box.appendChild(row);
    });
  }

  window.SensorChart = {open, close, isOpen: () => !dlg.hidden};
})();
