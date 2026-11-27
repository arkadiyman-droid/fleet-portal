/*
 * app/static/js/utility_study.js — one property's utilities as matrices
 * (templates/utility_study.html; data from app/utility_study.py, embedded
 * as JSON in #us-data). Dependency-free.
 *
 *   tiles        the last complete year per fuel, with the change
 *   bars         monthly cost, stacked by fuel
 *   year×month   one fuel: rows = years, columns = months (sequential blue)
 *   meters×month every account by month, with gaps / paid twice / flat marked
 *   year/year    each figure by year, colored by the change (blue down, red up)
 *   weather      gas vs heating degree-days, electric vs cooling degree-days
 *   table        the same numbers as a plain table
 */
(function () {
  "use strict";
  const D = JSON.parse(document.getElementById("us-data").textContent);
  const NS = "http://www.w3.org/2000/svg";
  const FUELS = ["water", "gas", "electric"];                 // categorical slots 1-3, in this order
  const FUEL_NAME = {electric: "Electric", gas: "Gas", water: "Water & sewer", apts: "Apartments"};
  const COLOR = {water: "#2a78d6", gas: "#eb6834", electric: "#1baf7a", apts: "#eda100"};
  const UNIT_PL = {kWh: "kWh", therm: "therms", kgal: "kgal"};
  const RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
                "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const M = D.months, N = M.length, LAST = D.last_full;
  const units = D.property.units;
  const tip = document.getElementById("us-tip");

  // ── formatting ────────────────────────────────────────────────────────────
  const nf0 = new Intl.NumberFormat("en-US", {maximumFractionDigits: 0});
  const nf1 = new Intl.NumberFormat("en-US", {maximumFractionDigits: 1});
  const nf2 = new Intl.NumberFormat("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  function compact(v) {
    if (v === null || v === undefined || !isFinite(v)) return "";
    if (v === 0) return "0";
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e4) return Math.round(v / 1e3) + "k";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "k";
    if (a >= 100) return nf0.format(v);
    if (a >= 10) return nf1.format(v);
    return v.toFixed(a >= 1 ? 2 : 3);
  }
  const money = v => (v === null || v === undefined) ? "—" : (v < 0 ? "-$" : "$") + nf0.format(Math.abs(v));
  const moneyC = v => (v === null || v === undefined) ? "" : (v < 0 ? "-$" : "$") + compact(Math.abs(v));
  const price = (v, u) => (v === null || v === undefined) ? "—" : "$" + (v >= 10 ? nf2.format(v) : v.toFixed(v >= 1 ? 2 : 3)) + "/" + u;
  const monthName = k => { const [y, m] = k.split("-"); return MON[+m - 1] + " " + y; };
  const esc = s => String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

  // ── color ─────────────────────────────────────────────────────────────────
  function hex(h) { return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)); }
  function mix(a, b, t) { const x = hex(a), y = hex(b); return `rgb(${x.map((c, i) => Math.round(c + (y[i] - c) * t))})`; }
  function seq(t) {                                            // 0..1 -> sequential blue
    t = Math.max(0, Math.min(1, t));
    const p = t * (RAMP.length - 1), i = Math.floor(p);
    return i >= RAMP.length - 1 ? RAMP[RAMP.length - 1] : mix(RAMP[i], RAMP[i + 1], p - i);
  }
  const inkOn = t => t > 0.55 ? "#fff" : "#0b0b0b";
  function div(c) {                                            // change -> blue (down) / gray / red (up)
    if (c === null || c === undefined || !isFinite(c)) return null;
    if (Math.abs(c) < 0.02) return "#f0efec";
    const k = Math.min(1, Math.abs(c) / 0.3);
    return c < 0 ? mix("#f0efec", "#256abf", 0.25 + 0.75 * k) : mix("#f0efec", "#d03b3b", 0.25 + 0.75 * k);
  }
  function scaler(vals) {                                      // robust range: 2nd..98th percentile
    const v = vals.filter(x => x !== null && x !== undefined && isFinite(x) && x > 0).sort((a, b) => a - b);
    if (!v.length) return () => 0;
    const lo = v[Math.floor(v.length * 0.02)], hi = v[Math.min(v.length - 1, Math.floor(v.length * 0.98))];
    return x => hi > lo ? (x - lo) / (hi - lo) * 0.92 + 0.04 : 0.5;
  }

  // ── tooltip (any element with data-tip) ───────────────────────────────────
  document.addEventListener("mouseover", e => {
    const t = e.target.closest("[data-tip]");
    if (!t) { tip.style.display = "none"; return; }
    tip.innerHTML = t.getAttribute("data-tip");
    tip.style.display = "block";
  });
  document.addEventListener("mousemove", e => {
    if (tip.style.display !== "block") return;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - 14;
    if (y + h > window.innerHeight - 8) y = e.clientY - h - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  });

  function buttons(el, opts, cur, onpick) {
    el.innerHTML = "";
    opts.forEach(([v, label]) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = label; b.setAttribute("aria-pressed", String(v === cur));
      b.addEventListener("click", () => { [...el.children].forEach(c => c.setAttribute("aria-pressed", "false"));
        b.setAttribute("aria-pressed", "true"); onpick(v); });
      el.appendChild(b);
    });
  }
  function svg(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function ticks(lo, hi, n) {
    if (!(hi > lo)) hi = lo + 1;
    const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }

  // ── tiles ─────────────────────────────────────────────────────────────────
  (function tiles() {
    const el = document.getElementById("us-tiles");
    const yrs = D.years.electric.map(r => r.year);
    const done = yrs.filter((y, i) => FUELS.some(g => D.years[g][i].complete));
    const ref = done.length ? done[done.length - 1] : yrs[yrs.length - 1];
    const at = (g, y) => D.years[g].find(r => r.year === y);
    let total = 0, fuelsNow = 0, totalPrev = 0;
    const html = [];
    ["electric", "gas", "water"].forEach(g => {
      const r = at(g, ref), p = at(g, ref - 1);
      if (!r || !r.cost) return;
      total += r.cost || 0; fuelsNow += r.cost || 0; totalPrev += (p && p.cost) || 0;
      const u = D.fuels[g].unit;
      const ch = p && p.cost ? r.cost / p.cost - 1 : null;
      html.push(`<div class="us-tile"><div class="l"><i style="background:${COLOR[g]}"></i>${FUEL_NAME[g]} ${ref}</div>
        <div class="n">${money(r.cost)}</div>
        <div class="s">${r.usage && r.price ? `${nf0.format(r.usage)} ${UNIT_PL[u]} · ${price(r.price, u)}` : (r.usage ? `${nf0.format(r.usage)} ${UNIT_PL[u]} (part of the year)` : "no usage on the bills")}
        ${r.per_apt ? `<br>${nf1.format(r.per_apt)} ${UNIT_PL[u]} per apartment` : ""}
        ${ch !== null ? `<br>cost ${ch >= 0 ? "+" : ""}${(ch * 100).toFixed(0)}% vs ${ref - 1}` : ""}</div></div>`);
    });
    const ap = D.apt_years.find(r => r.year === ref);
    if (ap && ap.cost) {
      total += ap.cost;
      html.push(`<div class="us-tile"><div class="l"><i style="background:${COLOR.apts}"></i>Apartments ${ref}</div>
        <div class="n">${money(ap.cost)}</div><div class="s">owner-paid apartment and vacant-unit electric</div></div>`);
    }
    html.unshift(`<div class="us-tile"><div class="l">All utilities ${ref}</div><div class="n">${money(total)}</div>
      <div class="s">${units ? money(total / units) + " per apartment" : "apartment count not set"}${totalPrev ? `<br>electric, gas and water ${fuelsNow / totalPrev - 1 >= 0 ? "+" : ""}${((fuelsNow / totalPrev - 1) * 100).toFixed(0)}% vs ${ref - 1}` : ""}</div></div>`);
    el.innerHTML = html.join("");
  })();

  // ── monthly cost, stacked ─────────────────────────────────────────────────
  (function bars() {
    const el = document.getElementById("us-bars");
    const series = FUELS.map(g => ({key: g, data: D.fuels[g].cost})).concat([{key: "apts", data: D.apartments}]);
    document.getElementById("us-bars-legend").innerHTML =
      series.map(s => `<span><i style="background:${COLOR[s.key]}"></i>${FUEL_NAME[s.key]}</span>`).join("") +
      `<span style="color:var(--muted)">faded = bills still coming in</span>`;
    const draw = () => {
      el.innerHTML = "";
      const W = Math.max(320, el.clientWidth), H = 220, m = {t: 8, r: 8, b: 24, l: 56};
      const s = svg("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Monthly cost by fuel"}, el);
      const tot = M.map((_, i) => series.reduce((a, x) => a + Math.max(0, x.data[i] || 0), 0));
      const yt = ticks(0, Math.max(1, ...tot), 4), top = yt[yt.length - 1];
      const iw = W - m.l - m.r, ih = H - m.t - m.b, bw = iw / N;
      const y = v => m.t + ih - v / top * ih;
      yt.forEach(v => {
        svg("line", {x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: "#eef0f3"}, s);
        svg("text", {x: m.l - 6, y: y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "#6b7280"}, s).textContent = moneyC(v);
      });
      M.forEach((k, i) => {
        const [yy, mm] = k.split("-");
        if (mm === "01" || (i === 0 && +mm <= 9)) {
          svg("text", {x: m.l + i * bw + 2, y: H - 6, "font-size": 11, fill: "#6b7280"}, s).textContent = mm === "01" ? yy : monthName(k);
          if (i) svg("line", {x1: m.l + i * bw, x2: m.l + i * bw, y1: m.t, y2: m.t + ih, stroke: "#e2e5ea"}, s);
        }
        let base = 0;
        const open = k > LAST;
        series.forEach(x => {
          const v = Math.max(0, x.data[i] || 0);
          if (!v) return;
          svg("rect", {x: m.l + i * bw + 1, y: y(base + v), width: Math.max(1, bw - 2), height: Math.max(0.5, y(base) - y(base + v)),
                       fill: COLOR[x.key], opacity: open ? 0.4 : 1, rx: Math.min(2, bw / 4)}, s);
          base += v;
        });
        const hit = svg("rect", {x: m.l + i * bw, y: m.t, width: bw, height: ih, fill: "transparent"}, s);
        hit.setAttribute("data-tip", `<b>${monthName(k)}</b>${open ? " (bills still coming in)" : ""}<br>` +
          series.map(x => `<span style="color:${COLOR[x.key]}">&#9632;</span> ${FUEL_NAME[x.key]}: ${money(x.data[i] || 0)}`).join("<br>") +
          `<br><b>Total ${money(tot[i])}</b>`);
      });
      svg("line", {x1: m.l, x2: W - m.r, y1: y(0), y2: y(0), stroke: "#9ca3af"}, s);
    };
    draw();
    window.addEventListener("resize", draw);
  })();

  // ── year × month ──────────────────────────────────────────────────────────
  const fuelsWithData = FUELS.filter(g => D.fuels[g].cost.some(v => v));
  const order = ["electric", "gas", "water"].filter(g => fuelsWithData.includes(g));
  const ym = {fuel: order[0] || "electric", metric: "usage"};
  function ymMetrics(g) {
    const u = D.fuels[g].unit, out = [["usage", UNIT_PL[u]], ["cost", "Cost"], ["price", "$ per " + u]];
    if (units) out.push(["apt", UNIT_PL[u] + " / apt / day"]);
    if (D.weather && g === "gas") out.push(["dd", "therms per heating degree-day"]);
    if (D.weather && g === "electric") out.push(["dd", "kWh per cooling degree-day"]);
    return out;
  }
  function ymValue(g, metric, i) {
    const f = D.fuels[g];
    if (metric === "cost") return f.cost[i] || null;
    if (metric === "usage") return f.usage[i];
    if (metric === "price") return f.price[i];
    if (metric === "apt") return f.per_apt_day[i];
    if (metric === "dd") {
      const dd = g === "gas" ? D.weather.hdd[i] : D.weather.cdd[i];
      return f.full[i] && f.usage[i] && dd >= 15 ? f.usage[i] / dd : null;
    }
    return null;
  }
  function drawYM() {
    const g = ym.fuel, metric = ym.metric, f = D.fuels[g], u = f.unit;
    const years = [...new Set(M.map(k => +k.slice(0, 4)))];
    const vals = M.map((_, i) => ymValue(g, metric, i));
    const sc = scaler(vals);
    const fmt = v => metric === "cost" ? moneyC(v) : metric === "price" ? "$" + (v >= 10 ? nf1.format(v) : v.toFixed(2)) : compact(v);
    const desc = {usage: `${UNIT_PL[u]}`, cost: "cost", price: `$ per ${u}`, apt: `${UNIT_PL[u]} per apartment per day`,
                  dd: g === "gas" ? "therms per heating degree-day" : "kWh per cooling degree-day"}[metric];
    let h = `<tr><th></th>${MON.map(x => `<th>${x}</th>`).join("")}<th style="text-align:right">Year</th></tr>`;
    years.forEach(y => {
      h += `<tr><th class="rh">${y}</th>`;
      let sum = 0, cnt = 0, costSum = 0, useSum = 0, allFull = true, months = 0;
      for (let mo = 1; mo <= 12; mo++) {
        const k = `${y}-${String(mo).padStart(2, "0")}`, i = M.indexOf(k);
        if (i < 0) { h += `<td class="none"></td>`; allFull = false; continue; }
        months++;
        const v = vals[i];
        const open = k > LAST, part = (metric === "usage" || metric === "price" || metric === "apt") && f.usage[i] && !f.full[i];
        if (!f.full[i]) allFull = false;
        costSum += f.cost[i] || 0; useSum += f.usage[i] || 0;
        const cls = [v === null || v === undefined ? "none" : "", part ? "part" : "", open ? "open" : ""].join(" ");
        const t = v === null || v === undefined ? 0 : sc(v);
        const wx = D.weather ? `<br>Heating degree-days ${D.weather.hdd[i] ?? "—"} · cooling ${D.weather.cdd[i] ?? "—"}` : "";
        const tipText = `<b>${FUEL_NAME[g]}, ${monthName(k)}</b>${open ? " (bills still coming in)" : ""}<br>Cost ${money(f.cost[i])}` +
          `<br>Usage ${f.usage[i] ? nf0.format(f.usage[i]) + " " + UNIT_PL[u] : "—"}${f.cover[i] !== null ? ` (on ${Math.round(f.cover[i] * 100)}% of the utility's bills)` : ""}` +
          `<br>Price ${price(f.price[i], u)}${f.per_apt_day[i] ? `<br>${f.per_apt_day[i].toFixed(2)} ${UNIT_PL[u]} per apartment per day` : ""}${wx}`;
        h += v === null || v === undefined
          ? `<td class="${cls}" data-tip="${esc(tipText)}">·</td>`
          : `<td class="${cls}" style="background:${seq(t)};color:${inkOn(t)}" data-tip="${esc(tipText)}">${fmt(v)}</td>`;
        if (v !== null && v !== undefined) { sum += v; cnt++; }
      }
      let tot = null;
      if (metric === "cost") tot = costSum || null;
      else if (metric === "usage") tot = useSum || null;
      else if (metric === "price") tot = allFull && useSum ? costSum / useSum : null;
      else if (cnt) tot = sum / cnt;
      const lab = metric === "usage" && !allFull && tot ? "*" : "";
      h += `<td class="tot">${tot === null ? "—" : (metric === "cost" ? money(tot) : metric === "price" ? "$" + tot.toFixed(2) : compact(tot)) + lab}${months < 12 ? ` <span style="color:var(--muted);font-weight:400">(${months} mo)</span>` : ""}</td></tr>`;
    });
    document.getElementById("ym").innerHTML = h;
    const lo = vals.filter(v => v > 0), mn = lo.length ? Math.min(...lo) : 0, mx = lo.length ? Math.max(...lo) : 0;
    document.getElementById("ym-key").innerHTML =
      `<span class="us-ramp">${fmt(mn) || "low"} <span class="bar" style="background:linear-gradient(90deg,${RAMP[0]},${RAMP[6]},${RAMP[12]})"></span> ${fmt(mx) || "high"} · ${desc}</span>` +
      `<span><span class="sw" style="background:#9ec5f4;background-image:repeating-linear-gradient(135deg,rgba(255,255,255,.55) 0 3px,transparent 3px 7px)"></span>usage on part of the bills</span>` +
      `<span><span class="sw" style="outline:1px dashed #9ca3af;outline-offset:-2px"></span>bills still coming in</span>` +
      (metric === "usage" ? `<span>* the year's usage is incomplete</span>` : "") +
      (metric === "dd" ? `<span>summer months with under 15 degree-days are left blank</span>` : "");
  }
  if (order.length) {
    buttons(document.getElementById("ym-fuel"), order.map(g => [g, FUEL_NAME[g]]), ym.fuel, v => {
      ym.fuel = v; if (!ymMetrics(v).some(x => x[0] === ym.metric)) ym.metric = "usage";
      buttons(document.getElementById("ym-metric"), ymMetrics(v), ym.metric, mm => { ym.metric = mm; drawYM(); });
      drawYM();
    });
    buttons(document.getElementById("ym-metric"), ymMetrics(ym.fuel), ym.metric, mm => { ym.metric = mm; drawYM(); });
    drawYM();
  }

  // ── meters × month ────────────────────────────────────────────────────────
  const mm = {fuel: "all", metric: "cost", scale: "all", span: 24};
  function drawMM() {
    const i0 = mm.span ? Math.max(0, N - mm.span) : 0, idx = [];
    for (let i = i0; i < N; i++) idx.push(i);
    const rows = D.meters.filter(r => mm.fuel === "all" || r.fuel === mm.fuel)
      .filter(r => idx.some(i => Math.abs(r.cost[i] || 0) >= 0.5 || r.usage[i]));   // nothing in this window: no row
    const val = (r, i) => mm.metric === "cost" ? (r.cost[i] || null) : r.usage[i];
    const whole = scaler(rows.flatMap(r => idx.map(i => val(r, i))));
    let h = `<tr><th></th>${idx.map(i => { const k = M[i], mo = +k.slice(5); return `<th title="${monthName(k)}">${mo === 1 || i === i0 ? MON[mo - 1] + " " + k.slice(2, 4) : MON[mo - 1].slice(0, 3)}</th>`; }).join("")}<th style="text-align:right">12 months</th></tr>`;
    let lastFuel = null;
    rows.forEach(r => {
      const sc = mm.scale === "row" ? scaler(idx.map(i => val(r, i))) : whole;
      const tags = [r.supplier ? "supply" : "", r.kind === "unit" ? "apartments" : r.kind === "vacant" ? "vacant apartments" :
                    r.kind === "fee" ? "fees" : r.kind === "other" ? "no meter named" : "", r.commodity === "sewer" ? "sewer" : ""].filter(Boolean);
      const sep = r.fuel !== lastFuel && lastFuel !== null ? " class=\"sep\"" : "";
      lastFuel = r.fuel;
      h += `<tr${sep}><th class="rh"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${COLOR[r.fuel]};margin-right:5px"></span>${esc(r.address || "—")}` +
        `<br><span class="b">${esc(r.utility)}${r.number ? " · " + esc(r.number) : ""}${tags.length ? " · " + tags.join(", ") : ""}${r.buildings.length ? " · " + esc(r.buildings.join(", ")) : ""}</span></th>`;
      let use12 = 0;
      idx.forEach(i => {
        const st = r.status[i], v = val(r, i), k = M[i], open = k > LAST;
        if (i >= N - 12) use12 += r.usage[i] || 0;
        const t = v === null || v === undefined ? 0 : sc(v);
        const cls = [st === "gap" ? "gap" : "", st === "twice" ? "twice" : "", st === "flat" ? "flat" : "", open ? "open" : "",
                     v === null || v === undefined ? (st === "gap" ? "" : "none") : ""].join(" ");
        const tipText = `<b>${esc(r.address || r.utility)}</b><br>${esc(r.utility)}${r.number ? " · " + esc(r.number) : ""}<br>${monthName(k)}: ${money(r.cost[i])}` +
          `${r.usage[i] ? " · " + nf0.format(r.usage[i]) + " " + UNIT_PL[r.unit] : ""}` +
          (st === "gap" ? "<br><b>No bill for this month</b>" : st === "twice" ? "<br><b>Paid twice?</b> Two invoices for about the same money" :
           st === "flat" ? "<br>A flat charge (no usage on it)" : "") + (open ? "<br>(bills still coming in)" : "");
        const txt = v === null || v === undefined ? (st === "gap" ? "gap" : "") : (mm.metric === "cost" ? moneyC(v) : compact(v));
        h += v === null || v === undefined
          ? `<td class="${cls}" data-tip="${esc(tipText)}">${txt}</td>`
          : `<td class="${cls}" style="background:${seq(t)};color:${inkOn(t)}" data-tip="${esc(tipText)}">${txt}</td>`;
      });
      h += `<td class="tot">${mm.metric === "cost" ? money(r.cost12) : (use12 ? compact(use12) + " " + (UNIT_PL[r.unit] || "") : "—")}</td></tr>`;
    });
    if (!rows.length) h += `<tr><td class="none" style="padding:10px">No accounts.</td></tr>`;
    document.getElementById("mm").innerHTML = h;
    document.getElementById("mm-key").innerHTML =
      `<span class="us-ramp">low <span class="bar" style="background:linear-gradient(90deg,${RAMP[0]},${RAMP[6]},${RAMP[12]})"></span> high${mm.scale === "row" ? " (each row on its own scale)" : ""}</span>` +
      `<span><span class="sw" style="outline:1.5px solid #d03b3b;outline-offset:-2px;background:#fff"></span>no bill (gap)</span>` +
      `<span><span class="sw" style="background:#9ec5f4;position:relative"></span><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#d03b3b;margin:0 4px 0 -2px"></span>paid twice?</span>` +
      `<span><b style="font-size:10px">F</b> flat charge</span><span>${rows.length} account${rows.length === 1 ? "" : "s"}</span>`;
  }
  buttons(document.getElementById("mm-fuel"), [["all", "All"]].concat(order.map(g => [g, FUEL_NAME[g]])), mm.fuel, v => { mm.fuel = v; drawMM(); });
  buttons(document.getElementById("mm-metric"), [["cost", "Cost"], ["usage", "Usage"]], mm.metric, v => { mm.metric = v; drawMM(); });
  buttons(document.getElementById("mm-scale"), [["all", "Whole matrix"], ["row", "Each row"]], mm.scale, v => { mm.scale = v; drawMM(); });
  buttons(document.getElementById("mm-span"), [[24, "Last 24"], [0, "All"]], mm.span, v => { mm.span = v; drawMM(); });
  drawMM();

  // ── year over year ────────────────────────────────────────────────────────
  (function yoy() {
    const years = D.years.electric.map(r => r.year);
    const rows = [];
    order.forEach(g => {
      const u = D.fuels[g].unit, Y = D.years[g];
      rows.push({g, label: `${FUEL_NAME[g]} cost`, get: r => r.cost, fmt: money, up: "bad"});
      rows.push({g, label: `${FUEL_NAME[g]} usage (${UNIT_PL[u]})`, get: r => r.price ? r.usage : null, fmt: v => nf0.format(v), up: "bad"});
      rows.push({g, label: `${FUEL_NAME[g]} $ per ${u}`, get: r => r.price, fmt: v => "$" + (v >= 10 ? nf2.format(v) : v.toFixed(3)), up: "bad"});
      if (units) rows.push({g, label: `${FUEL_NAME[g]} ${UNIT_PL[u]} per apartment`, get: r => r.per_apt, fmt: v => nf1.format(v), up: "bad"});
      rows.forEach(x => { if (!x.Y && x.g === g) x.Y = Y; });
    });
    if (D.apt_years.some(r => r.cost)) rows.push({g: "apts", label: "Apartments' electric cost", Y: D.apt_years.map((r, i) => ({...r, complete: D.years.electric[i].complete})), get: r => r.cost || null, fmt: money});
    let h = `<tr><th></th>${years.map((y, i) => `<th>${y}${D.years.electric[i].complete ? "" : " <span style='font-weight:400'>to date</span>"}</th>`).join("")}</tr>`;
    let last = null;
    rows.forEach(x => {
      h += `<tr${x.g !== last && last !== null ? " class=\"sep\"" : ""}><th class="rh"><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${COLOR[x.g]};margin-right:5px"></span>${x.label}</th>`;
      last = x.g;
      x.Y.forEach((r, i) => {
        const v = x.get(r), p = i ? x.get(x.Y[i - 1]) : null;
        const both = r.complete && i && x.Y[i - 1].complete && v !== null && p;
        const c = both ? v / p - 1 : null, bg = div(c);
        const t = v === null || v === undefined ? "—" : x.fmt(v);
        const tipText = `<b>${x.label}, ${r.year}</b><br>${t}${c !== null ? `<br>${c >= 0 ? "+" : ""}${(c * 100).toFixed(1)}% vs ${r.year - 1}` : (!r.complete ? "<br>year still in progress" : "")}`;
        h += `<td style="min-width:96px;${bg ? "background:" + bg + ";" : ""}${bg && Math.abs(c) > 0.2 ? "color:#fff;" : ""}" data-tip="${esc(tipText)}">${t}${c !== null ? `<br><span style="font-size:10px;opacity:.85">${c >= 0 ? "+" : ""}${(c * 100).toFixed(0)}%</span>` : ""}</td>`;
      });
      h += "</tr>";
    });
    document.getElementById("yoy").innerHTML = h;
  })();

  // ── weather ───────────────────────────────────────────────────────────────
  (function weather() {
    const sec = document.getElementById("us-wx"), sub = document.getElementById("us-wx-sub");
    if (!D.weather) {
      sub.textContent = "No weather data could be read for this property right now.";
      return;
    }
    const fits = D.weather.fits;
    sub.innerHTML = `Each dot is a month whose bills are all in and carry usage: use per day against degree-days per day (65 F base, ${esc(D.weather.place)}, from Open-Meteo). ` +
      `The line splits use into a <b>base load</b> (what the building uses with no heating or cooling: hot water, cooking, lights) and a part that follows the weather. A tight line (R² near 1) means the plant answers the weather; a scattered one means something else drives it.`;
    const parts = [["gas", "Gas against heating degree-days", "heating degree-days per day", "therms per day"],
                   ["electric", "Electric against cooling degree-days", "cooling degree-days per day", "kWh per day"]];
    let any = false;
    parts.forEach(([g, title, xl, yl]) => {
      const f = fits[g];
      const box = document.createElement("div");
      if (!f) {
        if (D.fuels[g].cost.some(v => v)) {
          box.innerHTML = `<h3 style="font-size:13px;margin:0 0 4px">${title}</h3><p class="card-sub">Not enough months with complete usage to draw a line.</p>`;
          sec.appendChild(box);
        }
        return;
      }
      any = true;
      box.innerHTML = `<h3 style="font-size:13px;margin:0 0 4px">${title}</h3><div class="us-legend"></div><div class="us-chart"></div>
        <p class="us-fit">Base load <b>${nf1.format(f.base)} ${g === "gas" ? "therms" : "kWh"}/day</b> (${nf0.format(f.base * 365)} a year) ·
        <b>${nf1.format(f.slope)} ${g === "gas" ? "therms" : "kWh"}</b> per degree-day · R² <b>${f.r2 === null ? "—" : f.r2.toFixed(2)}</b> ·
        weather-driven <b>${f.weather_share === null ? "—" : Math.round(f.weather_share * 100) + "%"}</b> of the use · ${f.n} months${f.left_out ? ` (${f.left_out} far off the line left out: hollow dots)` : ""}</p>`;
      sec.appendChild(box);
      const el = box.querySelector(".us-chart");
      const yrs = [...new Set(f.points.map(p => p[2].slice(0, 4)))].sort();
      const recent = yrs.slice(-3), PAL = ["#2a78d6", "#eb6834", "#1baf7a"];
      const col = y => recent.includes(y) ? PAL[recent.indexOf(y)] : "#9ca3af";
      box.querySelector(".us-legend").innerHTML = yrs.map(y => `<span><i style="background:${col(y)};border-radius:50%"></i>${y}</span>`).join("") +
        `<span><i style="background:#111827;height:2px;width:14px;vertical-align:3px"></i>fit</span>` +
        (f.left_out ? `<span><i style="background:#fff;border:1.5px solid #6b7280;border-radius:50%;width:8px;height:8px"></i>left out of the line</span>` : "");
      const draw = () => {
        el.innerHTML = "";
        const W = Math.max(300, el.clientWidth), H = 260, m = {t: 10, r: 10, b: 36, l: 56};
        const s = svg("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title}, el);
        const xs = f.points.map(p => p[0]), ys = f.points.filter(p => !p[3]).map(p => p[1]);
        const yCap = Math.max(...ys, f.base + f.slope * Math.max(...xs)) * 1.15;
        const xt = ticks(0, Math.max(...xs) * 1.05, 5), yt = ticks(0, yCap, 4);
        const X = v => m.l + v / xt[xt.length - 1] * (W - m.l - m.r), Y = v => m.t + (H - m.t - m.b) - v / yt[yt.length - 1] * (H - m.t - m.b);
        yt.forEach(v => { svg("line", {x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: "#eef0f3"}, s);
          svg("text", {x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "#6b7280"}, s).textContent = compact(v); });
        xt.forEach(v => svg("text", {x: X(v), y: H - 20, "text-anchor": "middle", "font-size": 11, fill: "#6b7280"}, s).textContent = compact(v) || "0");
        svg("text", {x: (m.l + W - m.r) / 2, y: H - 4, "text-anchor": "middle", "font-size": 11, fill: "#52514e"}, s).textContent = xl;
        const yl_ = svg("text", {x: 12, y: (m.t + H - m.b) / 2, "text-anchor": "middle", "font-size": 11, fill: "#52514e",
                                 transform: `rotate(-90 12 ${(m.t + H - m.b) / 2})`}, s);
        yl_.textContent = yl;
        svg("line", {x1: m.l, x2: W - m.r, y1: Y(0), y2: Y(0), stroke: "#9ca3af"}, s);
        const x1 = xt[xt.length - 1];
        svg("line", {x1: X(0), y1: Y(f.base), x2: X(x1), y2: Y(f.base + f.slope * x1), stroke: "#111827", "stroke-width": 2}, s);
        f.points.forEach(p => {
          const pred = f.base + f.slope * p[0], res = pred ? p[1] / pred - 1 : 0;
          const off = p[1] > yt[yt.length - 1], cy = off ? m.t + 4 : Y(p[1]);
          const c = svg("circle", p[3] ? {cx: X(p[0]), cy, r: 5, fill: "#fff", stroke: col(p[2].slice(0, 4)), "stroke-width": 2}
                                        : {cx: X(p[0]), cy, r: 5, fill: col(p[2].slice(0, 4)), stroke: "#fff", "stroke-width": 1.5}, s);
          c.setAttribute("data-tip", `<b>${monthName(p[2])}</b>${p[3] ? " (left out of the line)" : ""}${off ? " (above the chart)" : ""}<br>${nf1.format(p[0])} degree-days a day<br>${nf1.format(p[1])} ${g === "gas" ? "therms" : "kWh"} a day` +
            `<br>${res >= 0 ? "+" : ""}${(res * 100).toFixed(0)}% vs the line`);
        });
      };
      draw();
      window.addEventListener("resize", draw);
    });
    if (!any && !sec.children.length) sec.innerHTML = `<p class="card-sub">Not enough months with complete gas or electric usage.</p>`;
  })();

  // ── the plain table ───────────────────────────────────────────────────────
  (function table() {
    const el = document.getElementById("us-table");
    let h = `<table class="us-plain"><tr><th>Month</th>${order.map(g => `<th>${FUEL_NAME[g]} cost</th><th>${FUEL_NAME[g]} usage</th><th>on bills</th>`).join("")}<th>Apartments</th>${D.weather ? "<th>HDD</th><th>CDD</th>" : ""}</tr>`;
    M.forEach((k, i) => {
      h += `<tr><td>${monthName(k)}${k > LAST ? " *" : ""}</td>` + order.map(g => {
        const f = D.fuels[g];
        return `<td>${money(f.cost[i])}</td><td>${f.usage[i] ? nf0.format(f.usage[i]) + " " + UNIT_PL[f.unit] : "—"}</td><td>${f.cover[i] === null ? "—" : Math.round(f.cover[i] * 100) + "%"}</td>`;
      }).join("") + `<td>${money(D.apartments[i])}</td>` +
        (D.weather ? `<td>${D.weather.hdd[i] ?? "—"}</td><td>${D.weather.cdd[i] ?? "—"}</td>` : "") + "</tr>";
    });
    el.innerHTML = h + "</table><p class='card-sub'>* bills still coming in.</p>";
  })();
})();
