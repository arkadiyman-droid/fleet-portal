/*
 * app/static/js/charts.js — small, dependency-free charts for the history pages
 *
 * window.EchoCharts.line(el, {t, series, unit, height, range})
 *   range: optional [t0, t1] (ms), to line several charts up on one time axis.
 *   t: epoch ms; series: [{name, color, data, hidden}] (null = no value).
 *   Click a legend entry to show/hide it; hover for the values.
 * window.EchoCharts.bars(el, {t, series, unit, height, step})   stacked bars
 * window.EchoCharts.heat(el, {t, rows, v, req, height})         heat map
 *   rows: labels; v[row][col]; req[col] = the minimum that hour (or null):
 *   a cell under it is drawn red.
 *
 * Times are shown in New York time, as everywhere else in the portal.
 */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const TZ = "America/New_York";
  const fmt = {
    hm: new Intl.DateTimeFormat("en-US", {timeZone: TZ, hour: "numeric", minute: "2-digit"}),
    md: new Intl.DateTimeFormat("en-US", {timeZone: TZ, month: "short", day: "numeric"}),
    full: new Intl.DateTimeFormat("en-US", {timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}),
    day: new Intl.DateTimeFormat("en-US", {timeZone: TZ, weekday: "short", month: "short", day: "numeric"}),
  };
  const M = {top: 10, right: 12, bottom: 24, left: 42};

  function svg(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  function niceTicks(lo, hi, n) {
    if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
    if (lo === hi) { lo -= 1; hi += 1; }
    const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
    const out = [];
    for (let v = Math.floor(lo / step) * step; v <= hi + step * 0.001; v += step) out.push(Math.round(v * 1e6) / 1e6);
    if (out[out.length - 1] < hi) out.push(out[out.length - 1] + step);
    return out;
  }

  function timeTicks(t0, t1, width) {
    const span = t1 - t0, n = Math.max(2, Math.floor(width / 90));
    const steps = [3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3, 864e5, 2 * 864e5, 7 * 864e5, 14 * 864e5, 30 * 864e5];
    const step = steps.find(s => span / s <= n) || 60 * 864e5;
    const label = step < 864e5 ? (t => fmt.hm.format(t)) : (t => fmt.md.format(t));
    const out = [];
    // Ticks on whole New York hours / midnights: do the rounding in New York
    // "wall clock" milliseconds (UTC minus its 4 or 5 hour offset at t0).
    const localHour = parseInt(new Intl.DateTimeFormat("en-US", {timeZone: TZ, hour: "numeric", hourCycle: "h23"})
      .format(new Date(t0)), 10);
    const off = ((new Date(t0).getUTCHours() - localHour + 24) % 24) * 3600e3;
    for (let v = Math.ceil((t0 - off) / step) * step + off; v <= t1; v += step) out.push([v, label(v)]);
    return out;
  }

  function frame(el, height) {
    el.innerHTML = "";
    el.style.position = "relative";
    const W = Math.max(280, el.clientWidth || 600), H = height || 240;
    const s = svg("svg", {viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, style: "display:block;overflow:visible"}, el);
    return {s, W, H, iw: W - M.left - M.right, ih: H - M.top - M.bottom};
  }

  function tipBox(el) {
    const tip = document.createElement("div");
    tip.style.cssText = "position:absolute;pointer-events:none;background:rgba(17,24,39,.92);color:#fff;font-size:12px;" +
      "padding:6px 8px;border-radius:6px;white-space:nowrap;display:none;z-index:5;line-height:1.45";
    el.appendChild(tip);
    return tip;
  }

  function place(tip, el, x, y) {
    tip.style.display = "block";
    const w = tip.offsetWidth, W = el.clientWidth;
    tip.style.left = Math.min(Math.max(0, x + 12), W - w) + "px";
    tip.style.top = Math.max(0, y - 10) + "px";
  }

  function axes(g, f, x, y, yt, xt, unit) {
    yt.forEach(v => {
      svg("line", {x1: M.left, x2: M.left + f.iw, y1: y(v), y2: y(v), stroke: "#e5e7eb"}, g);
      svg("text", {x: M.left - 6, y: y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "#6b7280"}, g)
        .textContent = v + (unit || "");
    });
    xt.forEach(([v, l]) => {
      svg("line", {x1: x(v), x2: x(v), y1: M.top + f.ih, y2: M.top + f.ih + 4, stroke: "#9ca3af"}, g);
      svg("text", {x: x(v), y: M.top + f.ih + 17, "text-anchor": "middle", "font-size": 11, fill: "#6b7280"}, g)
        .textContent = l;
    });
  }

  function legend(el, series, redraw) {
    const box = document.createElement("div");
    box.style.cssText = "display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;margin:2px 0 6px " + M.left + "px";
    series.forEach(s => {
      const b = document.createElement("span");
      b.style.cssText = "cursor:pointer;user-select:none;opacity:" + (s.hidden ? .35 : 1);
      b.innerHTML = `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;` +
        `vertical-align:-1px;background:${s.color}"></span>`;
      b.appendChild(document.createTextNode(s.name));
      b.onclick = () => { s.hidden = !s.hidden; redraw(); };
      box.appendChild(b);
    });
    el.insertBefore(box, el.firstChild);
  }

  function nearest(t, v) {
    let lo = 0, hi = t.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (t[m] < v) lo = m; else hi = m; }
    return Math.abs(t[lo] - v) <= Math.abs(t[hi] - v) ? lo : hi;
  }

  function line(el, o) {
    const draw = () => {
      const f = frame(el, o.height), t = o.t || [];
      const shown = o.series.filter(s => !s.hidden && s.data && s.data.some(v => v !== null));
      legend(el, o.series.filter(s => s.data && s.data.some(v => v !== null)), draw);
      if (!t.length || !shown.length) {
        svg("text", {x: f.W / 2, y: f.H / 2, "text-anchor": "middle", fill: "#9ca3af", "font-size": 13}, f.s)
          .textContent = "No readings in this range yet";
        return;
      }
      let lo = Infinity, hi = -Infinity;
      shown.forEach(s => s.data.forEach(v => { if (v !== null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
      const yt = niceTicks(lo, hi, 5);
      const t0 = o.range ? o.range[0] : t[0], t1 = o.range ? o.range[1] : (t[t.length - 1] === t0 ? t0 + 3600e3 : t[t.length - 1]);
      const x = v => M.left + (v - t0) / (t1 - t0) * f.iw;
      const y = v => M.top + f.ih - (v - yt[0]) / (yt[yt.length - 1] - yt[0]) * f.ih;
      axes(f.s, f, x, y, yt, timeTicks(t0, t1, f.iw), o.unit);
      const gaps = []; for (let i = 1; i < t.length; i++) gaps.push(t[i] - t[i - 1]);
      gaps.sort((a, b) => a - b);
      const brk = (gaps[gaps.length >> 1] || 3600e3) * 3.5;
      shown.forEach(s => {
        let d = "", pen = false;
        s.data.forEach((v, i) => {
          if (v === null || (i && t[i] - t[i - 1] > brk)) { pen = false; if (v === null) return; }
          d += (pen ? "L" : "M") + x(t[i]).toFixed(1) + " " + y(v).toFixed(1);
          pen = true;
        });
        svg("path", {d, fill: "none", stroke: s.color, "stroke-width": s.width || 1.6, "stroke-linejoin": "round",
                     "stroke-dasharray": s.dash || ""}, f.s);
      });
      (o.marks || []).forEach(m => {
        if (m.t < t0 || m.t > t1) return;
        svg("line", {x1: x(m.t), x2: x(m.t), y1: M.top, y2: M.top + f.ih, stroke: m.color, "stroke-width": 1,
                     "stroke-dasharray": "3 3", opacity: .8}, f.s);
      });
      const cross = svg("line", {y1: M.top, y2: M.top + f.ih, stroke: "#111827", "stroke-width": 1, opacity: 0}, f.s);
      const tip = tipBox(el);
      const hit = svg("rect", {x: M.left, y: M.top, width: f.iw, height: f.ih, fill: "transparent"}, f.s);
      hit.addEventListener("mousemove", ev => {
        const r = f.s.getBoundingClientRect(), px = (ev.clientX - r.left) * f.W / r.width;
        const i = nearest(t, t0 + (px - M.left) / f.iw * (t1 - t0));
        cross.setAttribute("x1", x(t[i])); cross.setAttribute("x2", x(t[i])); cross.setAttribute("opacity", .35);
        tip.innerHTML = `<b>${fmt.full.format(t[i])}</b>` + shown.map(s => s.data[i] === null ? "" :
          `<br><span style="color:${s.color}">&#9632;</span> ${s.name}: ${Math.round(s.data[i] * 10) / 10}${o.unit || ""}`).join("") +
          (o.marks || []).filter(m => Math.abs(m.t - t[i]) < (t1 - t0) / f.iw * 4).map(m => `<br>${m.text}`).join("");
        place(tip, el, (ev.clientX - el.getBoundingClientRect().left), ev.clientY - el.getBoundingClientRect().top);
      });
      hit.addEventListener("mouseleave", () => { tip.style.display = "none"; cross.setAttribute("opacity", 0); });
    };
    draw();
    window.addEventListener("resize", draw);
  }

  function bars(el, o) {
    const draw = () => {
      const f = frame(el, o.height || 160), t = o.t || [];
      const shown = o.series.filter(s => !s.hidden);
      legend(el, o.series, draw);
      if (!t.length) {
        svg("text", {x: f.W / 2, y: f.H / 2, "text-anchor": "middle", fill: "#9ca3af", "font-size": 13}, f.s)
          .textContent = "No summaries yet: the first appears an hour after reading starts";
        return;
      }
      const step = o.step === "day" ? 864e5 : 3600e3;
      const tot = t.map((_, i) => shown.reduce((a, s) => a + (s.data[i] || 0), 0));
      const yt = niceTicks(0, Math.max(1, ...tot), 4);
      const t0 = o.range ? o.range[0] : t[0], t1 = o.range ? o.range[1] : t[t.length - 1] + step;
      const x = v => M.left + (v - t0) / (t1 - t0) * f.iw;
      const y = v => M.top + f.ih - v / yt[yt.length - 1] * f.ih;
      axes(f.s, f, x, y, yt, timeTicks(t0, t1, f.iw), o.unit);
      const bw = Math.max(1, f.iw / ((t1 - t0) / step) - (f.iw / ((t1 - t0) / step) > 4 ? 1 : 0));
      t.forEach((ti, i) => {
        let base = 0;
        shown.forEach(s => {
          const v = s.data[i] || 0;
          if (!v) return;
          svg("rect", {x: x(ti), y: y(base + v), width: bw, height: Math.max(0.5, y(base) - y(base + v)), fill: s.color}, f.s);
          base += v;
        });
      });
      const tip = tipBox(el);
      const hit = svg("rect", {x: M.left, y: M.top, width: f.iw, height: f.ih, fill: "transparent"}, f.s);
      hit.addEventListener("mousemove", ev => {
        const r = f.s.getBoundingClientRect(), px = (ev.clientX - r.left) * f.W / r.width;
        const i = nearest(t, t0 + (px - M.left) / f.iw * (t1 - t0) - step / 2);
        if (i < 0 || i >= t.length) return;
        tip.innerHTML = `<b>${(o.step === "day" ? fmt.day : fmt.full).format(t[i])}</b>` +
          shown.map(s => `<br><span style="color:${s.color}">&#9632;</span> ${s.name}: ${s.data[i] || 0}${o.unit || ""}`).join("");
        place(tip, el, ev.clientX - el.getBoundingClientRect().left, ev.clientY - el.getBoundingClientRect().top);
      });
      hit.addEventListener("mouseleave", () => { tip.style.display = "none"; });
    };
    draw();
    window.addEventListener("resize", draw);
  }

  function color(v, req) {
    if (v === null || v === undefined) return "#f3f4f6";
    if (req && v < req) return v < req - 4 ? "#991b1b" : "#dc2626";
    const stops = [[55, [37, 99, 235]], [64, [14, 165, 233]], [70, [34, 197, 94]], [76, [234, 179, 8]], [84, [234, 88, 12]]];
    if (v <= stops[0][0]) return `rgb(${stops[0][1]})`;
    for (let i = 1; i < stops.length; i++) {
      if (v <= stops[i][0]) {
        const [a, ca] = stops[i - 1], [b, cb] = stops[i], k = (v - a) / (b - a);
        return `rgb(${ca.map((c, j) => Math.round(c + (cb[j] - c) * k))})`;
      }
    }
    return `rgb(${stops[stops.length - 1][1]})`;
  }

  function heat(el, o) {
    const draw = () => {
      el.innerHTML = "";
      el.style.position = "relative";
      const rows = o.rows.length, cols = o.t.length;
      const W = Math.max(280, el.clientWidth || 600), rh = Math.max(7, Math.min(16, Math.floor(260 / Math.max(rows, 1))));
      const H = rows * rh + 22, iw = W - M.left - M.right;
      const c = document.createElement("canvas");
      const dpr = window.devicePixelRatio || 1;
      c.width = W * dpr; c.height = H * dpr; c.style.width = "100%"; c.style.height = H + "px"; c.style.display = "block";
      el.appendChild(c);
      const g = c.getContext("2d");
      g.scale(dpr, dpr);
      g.font = "11px sans-serif"; g.fillStyle = "#6b7280"; g.textAlign = "right"; g.textBaseline = "middle";
      const cw = iw / Math.max(cols, 1);
      for (let r = 0; r < rows; r++) {
        if (rh >= 10 || r % 2 === 0) g.fillText("#" + o.rows[r], M.left - 6, r * rh + rh / 2);
        for (let k = 0; k < cols; k++) {
          g.fillStyle = color(o.v[r][k], o.req ? o.req[k] : null);
          g.fillRect(M.left + k * cw, r * rh, Math.ceil(cw), rh - 1);
        }
        g.fillStyle = "#6b7280";
      }
      if (cols) {
        g.textAlign = "center";
        timeTicks(o.t[0], o.t[cols - 1] + 3600e3, iw).forEach(([v, l]) =>
          g.fillText(l, M.left + (v - o.t[0]) / 3600e3 * cw, rows * rh + 12));
      }
      const tip = tipBox(el);
      c.addEventListener("mousemove", ev => {
        const r = c.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width, py = (ev.clientY - r.top) * H / r.height;
        const k = Math.floor((px - M.left) / cw), row = Math.floor(py / rh);
        if (k < 0 || k >= cols || row < 0 || row >= rows) { tip.style.display = "none"; return; }
        const v = o.v[row][k], req = o.req ? o.req[k] : null;
        tip.innerHTML = `<b>Sensor #${o.rows[row]}</b> · ${fmt.full.format(o.t[k])}<br>` +
          (v === null ? "no reading" : `${v} F average`) + (req ? `<br>minimum this hour: ${req} F` +
          (v !== null && v < req ? " <b style='color:#fca5a5'>below</b>" : "") : "");
        place(tip, el, ev.clientX - el.getBoundingClientRect().left, ev.clientY - el.getBoundingClientRect().top);
      });
      c.addEventListener("mouseleave", () => { tip.style.display = "none"; });
    };
    draw();
    window.addEventListener("resize", draw);
  }

  window.EchoCharts = {line, bars, heat, color};
})();
