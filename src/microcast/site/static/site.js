// microcast status site: renders index.html and analytics.html from data.json.
// Plain SVG, no dependencies. Labels go in via textContent only.

const NS = "http://www.w3.org/2000/svg";

// ---- models: colors follow the model, never its rank or what else is shown --------------

// Fixed labels and color slots for models we know; any other model in data.json takes
// the next free slot (--s1..--s8), in name order, so its color never changes when
// models are hidden. raw_hrrr is the reference (gray), not a series.
const KNOWN = {
  raw_hrrr: { label: "Raw HRRR" },
  gbm_residual: { label: "GBM residual", slot: 1 },
  bias_rolling: { label: "Rolling bias", slot: 2 },
};
const MODELS = {};
let ORDER = []; // every model in the data: raw_hrrr first, then by color slot
const HIDDEN_KEY = "microcast.hiddenModels";
let hidden = new Set();
try { hidden = new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]")); } catch { /* private window */ }
const saveHidden = () => { try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hidden])); } catch { /* ignore */ } };

function registerModels(data) {
  const ids = [...new Set(data.leaderboard.map((r) => r.model))].sort();
  const used = new Set(Object.values(KNOWN).map((m) => m.slot).filter(Boolean));
  let next = 1;
  for (const id of ids) {
    const k = KNOWN[id] || {};
    let slot = k.slot;
    if (id !== "raw_hrrr" && !slot) {
      while (used.has(next)) next++;
      slot = next;
      used.add(slot);
    }
    MODELS[id] = {
      label: k.label || id.replaceAll("_", " "),
      slot: slot || 0,
      color: id === "raw_hrrr" ? "var(--ref)" : slot <= 8 ? `var(--s${slot})` : "var(--muted)",
    };
  }
  ORDER = ids.sort((a, b) => MODELS[a].slot - MODELS[b].slot);
}
const shown = (k) => !hidden.has(k);
const challengers = () => ORDER.filter((k) => k !== "raw_hrrr" && shown(k));
const withRaw = () => ORDER.filter(shown); // raw HRRR as a series (error charts, PIT)

// Dim every model mark except k inside scope (the whole page by default); null clears.
function focusModel(k, scope = document) {
  for (const e of scope.querySelectorAll("[data-model]")) e.classList.toggle("dim", !!k && e.dataset.model !== k);
}

// One row of toggles above the charts: click to show/hide a model everywhere, hover to
// highlight it everywhere. The choice is remembered in this browser.
function modelPicker(root, onChange) {
  const bar = el("div", { class: "picker", role: "group", "aria-label": "Models shown" }, root);
  el("span", { class: "picker-label", text: "Models" }, bar);
  const chips = [];
  for (const k of ORDER) {
    const b = el("button", { type: "button", class: "chip", "aria-pressed": String(shown(k)) }, bar);
    el("span", { class: "key", style: `background:${MODELS[k].color}` }, b);
    b.appendChild(document.createTextNode(MODELS[k].label));
    chips.push([k, b]);
    b.addEventListener("click", () => {
      hidden.has(k) ? hidden.delete(k) : hidden.add(k);
      b.setAttribute("aria-pressed", String(shown(k)));
      saveHidden();
      onChange();
    });
    for (const [on, off] of [["pointerenter", "pointerleave"], ["focus", "blur"]]) {
      b.addEventListener(on, () => shown(k) && focusModel(k));
      b.addEventListener(off, () => focusModel(null));
    }
  }
  const all = el("button", { type: "button", class: "chip ghost", text: "Show all" }, bar);
  all.addEventListener("click", () => {
    hidden.clear();
    for (const [, b] of chips) b.setAttribute("aria-pressed", "true");
    saveHidden();
    onChange();
  });
}

const el = (tag, attrs = {}, parent) => {
  const e = tag === "svg" || ["g", "line", "path", "circle", "rect", "text"].includes(tag)
    ? document.createElementNS(NS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") e.textContent = v;
    else if (k === "class") e.setAttribute("class", v);
    else e.setAttribute(k, v);
  }
  if (parent) parent.appendChild(e);
  return e;
};
const fmt = (v, d = 2) => (v == null || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const pct = (v, d = 0) => (v == null ? "–" : `${(100 * v).toFixed(d)}%`);
const signedPct = (v) => (v == null ? "–" : `${v >= 0 ? "+" : "−"}${Math.abs(100 * v).toFixed(1)}%`);
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("en-US", { timeZone: "America/Los_Angeles", dateStyle: "medium", timeStyle: "short" }) : "–");
const ago = (iso) => {
  if (!iso) return "–";
  const h = (Date.now() - new Date(iso)) / 36e5;
  return h < 48 ? `${h.toFixed(0)} h ago` : `${(h / 24).toFixed(0)} days ago`;
};

// ---- tooltip -------------------------------------------------------------------

const tip = el("div", { class: "tip", role: "status" }, document.body);
function showTip(evt, title, rows) {
  tip.replaceChildren();
  el("div", { class: "t", text: title }, tip);
  for (const r of rows) {
    const row = el("div", { class: `row${r.on ? " on" : ""}` }, tip);
    if (r.color) el("span", { class: "k", style: `background:${r.color}` }, row);
    el("b", { text: r.value }, row);
    el("span", { class: "n", text: r.name }, row);
  }
  tip.style.display = "block";
  const x = Math.min(evt.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.min(evt.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
const hideTip = () => (tip.style.display = "none");

// ---- shared chart scaffolding -----------------------------------------------------

function niceTicks(lo, hi, n = 5) {
  const span = hi - lo || 1;
  const step0 = span / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

function frame(parent, { title, w = 480, h = 240, m = { t: 12, r: 64, b: 28, l: 44 } }) {
  const card = el("div", { class: "card chart" }, parent);
  if (title) el("h3", { text: title }, card);
  const legend = el("div", { class: "legend" }, card);
  const svg = el("svg", { viewBox: `0 0 ${w} ${h}`, role: "img", "aria-label": title || "chart" }, card);
  return { card, legend, svg, w, h, m, iw: w - m.l - m.r, ih: h - m.t - m.b };
}

function legendFor(legend, keys, swatch = false) {
  for (const k of keys) {
    const item = el("span", MODELS[k] ? { "data-model": k, class: "item" } : {}, legend);
    if (MODELS[k]) {
      const card = legend.closest(".card");
      item.addEventListener("pointerenter", () => focusModel(k, card));
      item.addEventListener("pointerleave", () => focusModel(null, card));
    }
    el("span", { class: `key${swatch ? " sw" : ""}`, style: `background:${MODELS[k]?.color || k.color}` }, item);
    item.appendChild(document.createTextNode(MODELS[k]?.label || k.label));
  }
}

function yAxis(f, y, ticks, format) {
  const g = el("g", { class: "grid" }, f.svg);
  for (const t of ticks) {
    el("line", { x1: f.m.l, x2: f.m.l + f.iw, y1: y(t), y2: y(t) }, g);
    el("text", { x: f.m.l - 6, y: y(t) + 4, "text-anchor": "end", text: format(t) }, f.svg);
  }
}

function tableView(card, headers, rows) {
  const d = el("details", {}, card);
  el("summary", { text: "Table view" }, d);
  const wrap = el("div", { class: "scroll" }, d);
  const t = el("table", {}, wrap);
  const tr = el("tr", {}, el("thead", {}, t));
  headers.forEach((h, i) => el("th", { text: h, class: i ? "num" : "" }, tr));
  const tb = el("tbody", {}, t);
  for (const r of rows) {
    const row = el("tr", {}, tb);
    r.forEach((c, i) => el("td", { text: c, class: i ? "num" : "" }, row));
  }
}

// ---- line chart: x positions are categories (months, lead hours, local hours) -------------

function emptyCard(parent, title) {
  const card = el("div", { class: "card chart" }, parent);
  el("h3", { text: title }, card);
  el("p", { class: "sub", text: "No models selected. Use the Models toggles above." }, card);
}

function lineChart(parent, { title, xs, xLabel, series, yFormat, zeroLabel, tipFormat, every = 1 }) {
  if (!series.length) return emptyCard(parent, title);
  const f = frame(parent, { title });
  legendFor(f.legend, series.map((s) => s.key));
  const vals = series.flatMap((s) => s.values).filter((v) => v != null);
  let lo = Math.min(...vals, zeroLabel ? 0 : Infinity);
  let hi = Math.max(...vals, zeroLabel ? 0 : -Infinity);
  const pad = (hi - lo) * 0.1 || 0.1;
  lo -= pad; hi += pad;
  const ticks = niceTicks(lo, hi);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks.at(-1));
  const x = (i) => f.m.l + (xs.length === 1 ? f.iw / 2 : (i * f.iw) / (xs.length - 1));
  const y = (v) => f.m.t + f.ih - ((v - lo) / (hi - lo)) * f.ih;
  yAxis(f, y, ticks, yFormat);
  xs.forEach((xv, i) => {
    if (i % every === 0 || i === xs.length - 1)
      el("text", { x: x(i), y: f.h - 8, "text-anchor": "middle", text: xv }, f.svg);
  });
  if (xLabel) el("text", { x: f.m.l + f.iw, y: f.h - 8, "text-anchor": "start", dx: 8, text: xLabel }, f.svg);
  if (zeroLabel) {
    el("line", { class: "zero", x1: f.m.l, x2: f.m.l + f.iw, y1: y(0), y2: y(0) }, f.svg);
    el("text", { class: "direct", x: f.m.l + f.iw + 6, y: y(0) + 4, text: zeroLabel }, f.svg);
  }
  const ends = [];
  for (const s of series) {
    const color = MODELS[s.key].color;
    let d = "";
    s.values.forEach((v, i) => {
      if (v == null) return;
      d += `${d && s.values[i - 1] != null ? "L" : "M"}${x(i)},${y(v)}`;
    });
    el("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "data-model": s.key }, f.svg);
    const last = s.values.findLastIndex((v) => v != null);
    if (last >= 0) {
      el("circle", { cx: x(last), cy: y(s.values[last]), r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2, "data-model": s.key }, f.svg);
      ends.push({ x: x(last) + 8, y: y(s.values[last]) + 4, text: MODELS[s.key].label, key: s.key });
    }
  }
  // Direct end labels, nudged apart so they never overlap (<= 4 series only; beyond
  // that the legend and the hover highlight carry the names).
  if (series.length <= 4) {
    ends.sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 13);
    for (const e of ends) el("text", { class: "direct", x: e.x, y: e.y, text: e.text, "data-model": e.key }, f.svg);
  }
  // crosshair + one tooltip listing every series at that x
  const cross = el("line", { class: "crosshair", y1: f.m.t, y2: f.m.t + f.ih, visibility: "hidden" }, f.svg);
  const hit = el("rect", { x: f.m.l, y: f.m.t, width: f.iw, height: f.ih, fill: "transparent" }, f.svg);
  hit.addEventListener("pointermove", (e) => {
    const pt = f.svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const p = pt.matrixTransform(f.svg.getScreenCTM().inverse());
    const i = Math.max(0, Math.min(xs.length - 1, Math.round(((p.x - f.m.l) / f.iw) * (xs.length - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    // The series nearest the pointer is highlighted in the chart and in the tooltip.
    const near = series.filter((s) => s.values[i] != null)
      .reduce((best, s) => (!best || Math.abs(y(s.values[i]) - p.y) < Math.abs(y(best.values[i]) - p.y) ? s : best), null);
    if (series.length > 1) focusModel(near?.key, f.card);
    const rows = [...series].sort((a, b) => (b.values[i] ?? -Infinity) - (a.values[i] ?? -Infinity));
    showTip(e, `${xs[i]}${xLabel ? " " + xLabel : ""}`, rows.map((s) => ({
      color: MODELS[s.key].color, name: MODELS[s.key].label, value: (tipFormat || yFormat)(s.values[i]), on: s === near && series.length > 1,
    })));
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); focusModel(null, f.card); });
  tableView(f.card, [xLabel || "x", ...series.map((s) => MODELS[s.key].label)],
    xs.map((xv, i) => [String(xv), ...series.map((s) => (tipFormat || yFormat)(s.values[i]))]));
  return f;
}

// ---- dot + 95% CI whisker, one row per category -----------------------------------------

function dotChart(parent, { title, rows, keys, format }) {
  if (!keys.length) return emptyCard(parent, title);
  const rowH = Math.max(30, keys.length * 9 + 12);
  const f = frame(parent, { title, h: rows.length * rowH + 40, m: { t: 8, r: 24, b: 28, l: 72 } });
  legendFor(f.legend, keys);
  const vals = rows.flatMap((r) => keys.flatMap((k) => [r[k]?.lo, r[k]?.hi, r[k]?.v])).filter((v) => v != null);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const ticks = niceTicks(lo, hi, 4);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks.at(-1));
  const x = (v) => f.m.l + ((v - lo) / (hi - lo)) * f.iw;
  const g = el("g", { class: "grid" }, f.svg);
  for (const t of ticks) {
    el("line", { x1: x(t), x2: x(t), y1: f.m.t, y2: f.m.t + f.ih }, g);
    el("text", { x: x(t), y: f.h - 8, "text-anchor": "middle", text: format(t) }, f.svg);
  }
  el("line", { class: "zero", x1: x(0), x2: x(0), y1: f.m.t, y2: f.m.t + f.ih }, f.svg);
  rows.forEach((r, i) => {
    const yc = f.m.t + i * rowH + rowH / 2;
    el("text", { x: f.m.l - 8, y: yc + 4, "text-anchor": "end", text: r.label, class: "direct" }, f.svg);
    keys.forEach((k, j) => {
      const d = r[k];
      if (!d) return;
      const yy = yc + (j - (keys.length - 1) / 2) * 9;
      const color = MODELS[k].color;
      el("line", { x1: x(d.lo), x2: x(d.hi), y1: yy, y2: yy, stroke: color, "stroke-width": 2, "stroke-linecap": "round", "data-model": k }, f.svg);
      const c = el("circle", { cx: x(d.v), cy: yy, r: 4.5, fill: color, stroke: "var(--surface)", "stroke-width": 2, tabindex: 0, "data-model": k }, f.svg);
      const show = (e) => { focusModel(k, f.card); showTip(e, r.label, [{ color, name: MODELS[k].label, value: `${format(d.v)} [${format(d.lo)}, ${format(d.hi)}]` }]); };
      c.addEventListener("pointermove", show);
      c.addEventListener("pointerleave", () => { hideTip(); focusModel(null, f.card); });
    });
  });
  tableView(f.card, ["", ...keys.flatMap((k) => [MODELS[k].label, "95% CI"])],
    rows.map((r) => [r.label, ...keys.flatMap((k) => (r[k] ? [format(r[k].v), `${format(r[k].lo)} to ${format(r[k].hi)}`] : ["–", "–"]))]));
}

// ---- bars (PIT histogram) -----------------------------------------------------------

function barChart(parent, { title, labels, values, color, refValue, refLabel, format, model }) {
  const f = frame(parent, { title, h: 180, m: { t: 8, r: 56, b: 28, l: 40 } });
  if (model) f.card.dataset.model = model;
  const hi = Math.max(...values, refValue || 0) * 1.15;
  const ticks = niceTicks(0, hi, 3);
  const top = Math.max(hi, ticks.at(-1));
  const y = (v) => f.m.t + f.ih - (v / top) * f.ih;
  yAxis(f, y, ticks, format);
  const bw = f.iw / labels.length;
  values.forEach((v, i) => {
    const x0 = f.m.l + i * bw + 1, w = bw - 2, y0 = y(v), hgt = f.m.t + f.ih - y0;
    const r = Math.min(4, w / 2, hgt);
    // rounded data end, square baseline end
    const d = `M${x0},${f.m.t + f.ih}V${y0 + r}Q${x0},${y0} ${x0 + r},${y0}H${x0 + w - r}Q${x0 + w},${y0} ${x0 + w},${y0 + r}V${f.m.t + f.ih}Z`;
    const p = el("path", { d, fill: color }, f.svg);
    p.addEventListener("pointermove", (e) => showTip(e, `PIT ${labels[i]}`, [{ color, name: "of forecasts", value: format(v) }]));
    p.addEventListener("pointerleave", hideTip);
  });
  el("line", { class: "axis", x1: f.m.l, x2: f.m.l + f.iw, y1: f.m.t + f.ih, y2: f.m.t + f.ih }, f.svg);
  if (refValue != null) {
    el("line", { class: "zero", x1: f.m.l, x2: f.m.l + f.iw, y1: y(refValue), y2: y(refValue) }, f.svg);
    el("text", { class: "direct", x: f.m.l + f.iw + 6, y: y(refValue) + 4, text: refLabel }, f.svg);
  }
  [0, labels.length - 1].forEach((i) => el("text", { x: f.m.l + i * bw + bw / 2, y: f.h - 8, "text-anchor": "middle", text: labels[i] }, f.svg));
  tableView(f.card, ["PIT bin", "share"], labels.map((l, i) => [l, format(values[i])]));
}

// ---- heatmap (diverging) -----------------------------------------------------------

function mix(a, b, t) {
  const pa = a.match(/\w\w/g).map((h) => parseInt(h, 16)), pb = b.match(/\w\w/g).map((h) => parseInt(h, 16));
  return `rgb(${pa.map((v, i) => Math.round(v + (pb[i] - v) * t)).join(",")})`;
}
function heatmap(parent, { title, rows, cols, value, n, unit, limit, labels }) {
  const css = getComputedStyle(document.documentElement);
  const [cold, mid, warm] = ["--div-cold", "--div-mid", "--div-warm"].map((v) => css.getPropertyValue(v).trim());
  const f = frame(parent, { title, w: 640, h: rows.length * 22 + 50, m: { t: 8, r: 8, b: 28, l: 64 } });
  for (const [label, c] of [[labels[0], cold], [labels[1], warm]]) {
    const item = el("span", {}, f.legend);
    el("span", { class: "key sw", style: `background:${c}` }, item);
    item.appendChild(document.createTextNode(label));
  }
  const cw = f.iw / cols.length, ch = 20;
  rows.forEach((r, i) => {
    el("text", { x: f.m.l - 8, y: f.m.t + i * 22 + 14, "text-anchor": "end", text: r }, f.svg);
    cols.forEach((c, j) => {
      const v = value(r, c);
      if (v == null) return;
      const t = Math.max(-1, Math.min(1, v / limit));
      const fill = t < 0 ? mix(mid, cold, -t) : mix(mid, warm, t);
      const cell = el("rect", { x: f.m.l + j * cw + 1, y: f.m.t + i * 22, width: cw - 2, height: ch, rx: 2, fill, tabindex: 0 }, f.svg);
      cell.addEventListener("pointermove", (e) => showTip(e, `${r}, ${c}:00 PT`, [
        { name: unit + " obs − HRRR", value: `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}` },
        { name: "forecasts", value: String(n(r, c)) },
      ]));
      cell.addEventListener("pointerleave", hideTip);
    });
  });
  cols.forEach((c, j) => { if (j % 3 === 0) el("text", { x: f.m.l + j * cw + cw / 2, y: f.h - 8, "text-anchor": "middle", text: `${c}h` }, f.svg); });
  tableView(f.card, ["month", ...cols.map((c) => `${c}h`)], rows.map((r) => [r, ...cols.map((c) => fmt(value(r, c)))]));
}

// ---- pages ------------------------------------------------------------------------

function gateTiles(root, data) {
  const tiles = el("div", { class: "tiles" }, root);
  for (const g of data.gate) {
    const t = el("div", { class: "card tile" }, tiles);
    el("div", { class: "label", text: `${data.targets[g.variable].label} · GBM residual vs raw HRRR` }, t);
    const near = g.buckets.find((b) => b.lead === "1-3 h");
    el("div", { class: "value", text: signedPct(near?.skill) }, t);
    el("div", { class: "detail", text: g.buckets.map((b) => `${b.lead}: ${signedPct(b.skill)} [${signedPct(b.skill_lo)}, ${signedPct(b.skill_hi)}]`).join(" · ") }, t);
    const s = el("div", { class: `status ${g.passed ? "pass" : "fail"}` }, t);
    el("span", { class: "dot", "aria-hidden": "true" }, s);
    s.appendChild(document.createTextNode(g.passed ? "✓ Phase 1 gate passed" : "✗ Gate not met"));
  }
}

function leaderboardTable(root, data) {
  const card = el("div", { class: "card scroll" }, root);
  const t = el("table", {}, card);
  const head = ["Target", "Lead", "Model", "CRPS", "MAE", "Bias", "80% cover", "Skill vs raw", "95% CI", "n"];
  const tr = el("tr", {}, el("thead", {}, t));
  head.forEach((h, i) => el("th", { text: h, class: i > 2 ? "num" : "" }, tr));
  const tb = el("tbody", {}, t);
  const rows = data.leaderboard.filter((r) => r.point === "all" && shown(r.model))
    .sort((a, b) => a.variable.localeCompare(b.variable) || a.lead.localeCompare(b.lead) || b.skill - a.skill);
  for (const r of rows) {
    const row = el("tr", { "data-model": r.model }, tb);
    const unit = data.targets[r.variable].unit;
    [data.targets[r.variable].label, r.lead, MODELS[r.model].label, `${fmt(r.crps)} ${unit}`, fmt(r.mae), fmt(r.bias),
      pct(r.coverage_80), r.model === "raw_hrrr" ? "baseline" : signedPct(r.skill),
      r.model === "raw_hrrr" ? "" : `${signedPct(r.skill_lo)} to ${signedPct(r.skill_hi)}`, r.n.toLocaleString()]
      .forEach((c, i) => el("td", { text: c, class: i > 2 ? "num" : "" }, row));
  }
}

function monthlyCharts(root, data) {
  const grid = el("div", { class: "multiples" }, root);
  for (const v of Object.keys(data.targets)) {
    const rows = data.monthly.filter((r) => r.variable === v);
    const xs = [...new Set(rows.map((r) => r.month))].sort();
    lineChart(grid, {
      title: `${data.targets[v].label}: skill vs raw HRRR by month`,
      xs: xs.map((m) => m.slice(2)), every: 2,
      series: challengers().map((k) => ({ key: k, values: xs.map((m) => rows.find((r) => r.model === k && r.month === m)?.skill ?? null) })),
      yFormat: (t) => `${Math.round(100 * t)}%`, tipFormat: signedPct, zeroLabel: "raw HRRR",
    });
  }
}

function freshnessTables(root, data) {
  const fr = data.freshness;
  const card = el("div", { class: "card scroll" }, root);
  el("p", { class: "sub", text: `HRRR cycles ${fmtTime(fr.hrrr.first)} → ${fmtTime(fr.hrrr.last)} (${fr.hrrr.cycles.toLocaleString()} cycles; latest ${ago(fr.hrrr.last)})` }, card);
  const t = el("table", {}, card);
  const tr = el("tr", {}, el("thead", {}, t));
  ["Station", "Source", "First obs", "Latest obs", "Rows", "QC flagged"].forEach((h, i) => el("th", { text: h, class: i > 3 ? "num" : "" }, tr));
  const tb = el("tbody", {}, t);
  for (const s of fr.stations) {
    const src = data.stations.find((x) => x.id === s.station_id)?.source || "";
    const row = el("tr", {}, tb);
    [s.station_id, src, fmtTime(s.first), `${fmtTime(s.last)} (${ago(s.last)})`, s.rows.toLocaleString(), pct(s.flagged, 1)]
      .forEach((c, i) => el("td", { text: c, class: i > 3 ? "num" : "" }, row));
  }
  const card2 = el("div", { class: "card scroll", style: "margin-top:12px" }, root);
  const t2 = el("table", {}, card2);
  const tr2 = el("tr", {}, el("thead", {}, t2));
  ["Lake table", "Rows", "Files", "Snapshots", "Updated"].forEach((h, i) => el("th", { text: h, class: i ? "num" : "" }, tr2));
  const tb2 = el("tbody", {}, t2);
  for (const r of fr.tables) {
    const row = el("tr", {}, tb2);
    [r.table, r.rows.toLocaleString(), r.files.toLocaleString(), String(r.snapshots), fmtTime(r.updated)]
      .forEach((c, i) => el("td", { text: c, class: i ? "num" : "" }, row));
  }
}

// Clear a section and return it, for re-rendering after the model selection changes.
const section = (id) => { const e = document.getElementById(id); e.replaceChildren(); return e; };

function renderIndex(data) {
  gateTiles(document.getElementById("gate"), data);
  const draw = () => {
    leaderboardTable(section("leaderboard"), data);
    monthlyCharts(section("monthly"), data);
  };
  modelPicker(document.getElementById("models"), draw);
  draw();
  freshnessTables(document.getElementById("freshness"), data);
}

function renderAnalytics(data) {
  modelPicker(document.getElementById("models"), () => renderModelSections(data));
  renderModelSections(data);
  rawBiasHeatmap(data);
}

function renderModelSections(data) {
  const A = data.analytics;
  const targets = Object.keys(data.targets);
  const CHALLENGERS = challengers();

  const byLead = el("div", { class: "multiples" }, section("by-lead"));
  for (const v of targets) {
    const rows = A.by_lead.filter((r) => r.variable === v);
    const xs = [...new Set(rows.map((r) => r.lead_h))].sort((a, b) => a - b);
    lineChart(byLead, {
      title: `${data.targets[v].label}: skill by lead`, xs, xLabel: "h",
      series: CHALLENGERS.map((k) => ({ key: k, values: xs.map((h) => rows.find((r) => r.model === k && r.lead_h === h)?.skill ?? null) })),
      yFormat: (t) => `${Math.round(100 * t)}%`, tipFormat: signedPct, zeroLabel: "raw HRRR",
    });
  }

  const byStation = el("div", { class: "multiples wide" }, section("by-station"));
  for (const v of targets) {
    for (const lead of ["1-3 h", "4-6 h"]) {
      const rows = data.stations.map((s) => {
        const out = { label: s.id };
        for (const k of CHALLENGERS) {
          const r = data.leaderboard.find((x) => x.variable === v && x.point === s.id && x.model === k && x.lead === lead);
          if (r) out[k] = { v: r.skill, lo: r.skill_lo, hi: r.skill_hi };
        }
        return out;
      }).filter((r) => CHALLENGERS.some((k) => r[k]));
      if (rows.length || !CHALLENGERS.length) dotChart(byStation, { title: `${data.targets[v].label}, ${lead}: skill by station`, rows, keys: CHALLENGERS, format: (t) => `${Math.round(100 * t)}%` });
    }
  }

  const byHour = el("div", { class: "multiples" }, section("by-hour"));
  for (const v of targets) {
    const rows = A.by_hour.filter((r) => r.variable === v);
    const xs = [...Array(24).keys()];
    lineChart(byHour, {
      title: `${data.targets[v].label}: mean absolute error by hour (PT)`, xs, xLabel: "h", every: 3,
      series: withRaw().map((k) => ({ key: k, values: xs.map((h) => rows.find((r) => r.model === k && r.local_hour === h)?.mae ?? null) })),
      yFormat: (t) => fmt(t, 1), tipFormat: (t) => `${fmt(t)} ${data.targets[v].unit}`,
    });
  }

  const pit = el("div", { class: "multiples" }, section("pit"));
  for (const v of targets) {
    for (const k of withRaw()) {
      const rows = A.pit.filter((r) => r.variable === v && r.model === k);
      const total = rows.reduce((a, r) => a + r.n, 0);
      if (!total) continue;
      const values = [...Array(10).keys()].map((b) => (rows.find((r) => r.bin === b)?.n || 0) / total);
      barChart(pit, {
        title: `${data.targets[v].label} · ${MODELS[k].label}`, labels: [...Array(10).keys()].map((b) => `${b / 10}–${(b + 1) / 10}`),
        values, color: MODELS[k].color, refValue: 0.1, refLabel: "calibrated", format: (t) => pct(t), model: k,
      });
    }
  }
}

function rawBiasHeatmap(data) {
  const A = data.analytics;
  const targets = Object.keys(data.targets);
  const hm = document.getElementById("raw-bias");
  const controls = el("div", { class: "controls" }, hm);
  const lab = el("label", { text: "Station " }, controls);
  const sel = el("select", {}, lab);
  const tsel = el("select", {}, el("label", { text: "Target " }, controls));
  for (const s of data.stations) el("option", { value: s.id, text: s.id }, sel);
  for (const v of targets) el("option", { value: v, text: data.targets[v].label }, tsel);
  const holder = el("div", {}, hm);
  const draw = () => {
    holder.replaceChildren();
    const rows = A.raw_bias.filter((r) => r.point_id === sel.value && r.variable === tsel.value);
    if (!rows.length) { el("p", { class: "sub", text: "No observations of this target at this station." }, holder); return; }
    const months = [...new Set(rows.map((r) => r.month))].sort();
    const get = (m, h) => rows.find((r) => r.month === m && r.local_hour === h);
    const limit = Math.max(0.5, ...rows.map((r) => Math.abs(r.obs_minus_hrrr))) * 0.8;
    heatmap(holder, {
      title: `${sel.value}: observed minus HRRR, ${data.targets[tsel.value].label.toLowerCase()} (${data.targets[tsel.value].unit})`,
      rows: months, cols: [...Array(24).keys()], value: (m, h) => get(m, h)?.obs_minus_hrrr ?? null,
      n: (m, h) => get(m, h)?.n ?? 0, unit: data.targets[tsel.value].unit, limit,
      labels: tsel.value === "gust" ? ["HRRR too gusty", "HRRR too calm"] : ["HRRR too warm", "HRRR too cold"],
    });
  };
  sel.value = "SFOC1";
  sel.addEventListener("change", draw);
  tsel.addEventListener("change", draw);
  draw();
}

function footer(data) {
  const f = document.querySelector("footer");
  const b = data.backtest;
  f.textContent = `Generated ${fmtTime(data.generated_at)} PT · backtest ${fmtTime(b.generated_at)} · code ${b.git_sha} · gold snapshot ${b.gold_snapshot_id}`;
}

fetch("data.json")
  .then((r) => r.json())
  .then((data) => {
    registerModels(data);
    (document.body.dataset.page === "analytics" ? renderAnalytics : renderIndex)(data);
    footer(data);
  })
  .catch((err) => {
    const m = document.querySelector("main");
    el("p", { class: "sub", text: `Could not load data.json: ${err}` }, m);
  });
