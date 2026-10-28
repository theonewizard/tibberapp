/* Home Energy web UI. Talks only to this app's /v1 API. No build step. */
"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel) => document.querySelector(sel);
const view = () => $("#view");

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

// replaceChildren() that skips null/false children (optional tiles).
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

class ApiError extends Error {
  constructor(status, problem) { super(problem?.detail || problem?.title || `HTTP ${status}`); this.status = status; this.problem = problem; }
}

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", headers: { Accept: "application/json" }, ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  if (r.status === 204) return null;
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) {
    if (r.status === 401 && !path.startsWith("/v1/session")) { location.hash = "#/login"; }
    throw new ApiError(r.status, body);
  }
  return body;
}

const nf = (d = 1) => new Intl.NumberFormat(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
const fmtNum = (v, d = 1) => (v === null || v === undefined ? "–" : nf(d).format(v));
function fmtMoney(v, currency, d = 2) {
  if (v === null || v === undefined) return "–";
  if (Math.abs(v) >= 1000) d = 0;
  if (!currency) return nf(d).format(v);
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: d }).format(v); }
  catch { return `${nf(d).format(v)} ${currency}`; }
}
function fmtTime(iso, tz, opts = { hour: "2-digit", minute: "2-digit" }) {
  return new Intl.DateTimeFormat(undefined, { timeZone: tz, ...opts }).format(new Date(iso));
}
const LEVEL_LABEL = { VERY_CHEAP: "Very cheap", CHEAP: "Cheap", NORMAL: "Normal", EXPENSIVE: "Expensive", VERY_EXPENSIVE: "Very expensive" };
const LEVEL_VAR = { VERY_CHEAP: "--lvl-very-cheap", CHEAP: "--lvl-cheap", NORMAL: "--lvl-normal", EXPENSIVE: "--lvl-expensive", VERY_EXPENSIVE: "--lvl-very-expensive" };
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const levelBadge = (lvl) => (lvl ? h("span", { class: "level", "data-level": lvl }, LEVEL_LABEL[lvl]) : null);

function tile(label, value, unit, sub, cls = "") {
  return h("div", { class: `card tile ${cls}` }, h("div", { class: "label" }, label),
    h("div", { class: "value" }, value, unit ? h("span", { class: "unit" }, unit) : null),
    sub ? h("div", { class: "sub" }, sub) : null);
}

function showStale(meta) {
  const el = $("#stale");
  if (meta?.stale) {
    const since = meta.staleSince ? new Date(meta.staleSince).toLocaleString() : "unknown";
    el.textContent = `Tibber did not respond; showing data stale since ${since}.`;
    el.hidden = false;
  }
}

function errorCard(err) {
  if (err instanceof ApiError && err.status === 409) {
    return h("div", { class: "card" }, h("p", {}, err.message), h("a", { href: "#/settings" }, "Open Settings"));
  }
  return h("div", { class: "card error" }, `Could not load: ${err.message}`);
}

// Track timers/streams per screen so navigation cleans them up.
let cleanups = [];
const onLeave = (fn) => cleanups.push(fn);
const charts = [];
function chart(canvas, config) {
  const c = new Chart(canvas, config);
  charts.push(c);
  return c;
}
function baseOptions(extra = {}) {
  const text = cssVar("--text-secondary"), grid = cssVar("--grid");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false }, tooltip: { displayColors: false } },
    scales: {
      x: { ticks: { color: text, maxRotation: 0, autoSkip: true, autoSkipPadding: 12 }, grid: { display: false } },
      y: { ticks: { color: text }, grid: { color: grid }, border: { display: false }, beginAtZero: true },
    },
    ...extra,
  };
}

// ------------------------------------------------------------------ state
const state = { homes: [], homeId: store.get("homeId"), homesMeta: null };
const home = () => state.homes.find((x) => x.id === state.homeId);

async function loadHomes() {
  const body = await api("/v1/homes");
  state.homes = body.homes;
  state.homesMeta = body.meta;
  if (!state.homes.some((x) => x.id === state.homeId)) state.homeId = state.homes[0]?.id || null;
  const sel = $("#home-select");
  fill(sel, ...state.homes.map((x) => h("option", { value: x.id, selected: x.id === state.homeId }, x.name)));
  $(".home-picker").hidden = state.homes.length < 2;
}
$("#home-select").addEventListener("change", (e) => { state.homeId = e.target.value; store.set("homeId", state.homeId); route(); });

// ------------------------------------------------------------------ screens
const screens = {};

screens.login = async () => {
  $(".tabs").hidden = true;
  const pw = h("input", { type: "password", autocomplete: "current-password", required: true, id: "pw" });
  const msg = h("p", { class: "error", role: "alert" });
  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    try { await api("/v1/session", { json: { password: pw.value } }); $(".tabs").hidden = false; location.hash = "#/home"; }
    catch (err) { msg.textContent = err.status === 401 ? "Wrong password." : err.message; }
  } }, h("label", { for: "pw" }, "Password"), pw, h("div", { class: "row" }), h("button", { class: "primary", type: "submit" }, "Sign in"), msg);
  fill(view(), h("h1", {}, "Sign in"), form);
  pw.focus();
};

screens.home = async () => {
  const hm = home();
  if (!hm) return noHomes();
  const out = [h("h1", {}, hm.name)];
  const priceCard = h("div", { class: "grid" });
  const liveCard = h("div", { class: "grid" });
  const chartCard = h("div", { class: "card" }, h("h2", {}, "Price today and tomorrow"));
  const devCard = h("div", {});
  out.push(priceCard, liveCard, chartCard, devCard);
  fill(view(), ...out);

  const prices = api(`/v1/homes/${hm.id}/prices?resolution=quarter_hourly`).then((p) => {
    showStale(p.meta);
    if (!p.available) { fill(priceCard, h("div", { class: "card" }, "No price data for this account")); chartCard.hidden = true; return; }
    const c = p.current;
    fill(priceCard, 
      tile("Price now", c ? fmtMoney(c.total, p.currency, 2) : "–", "/kWh", c ? levelBadge(c.level) : null, "hero"));
    priceChart(chartCard, p, "quarter_hourly");
  }).catch((e) => fill(priceCard, errorCard(e)));

  // Today so far: live stream if a Pulse/Watty is paired, otherwise hourly consumption (lags).
  const today = startOfLocalDay(hm.timeZone);
  const fillToday = (kwh, cost, currency, note) => fill(liveCard, 
    tile("Used today", fmtNum(kwh, 2), "kWh", note), tile("Cost today", fmtMoney(cost, currency), null, note));
  if (hm.realTimeConsumption) {
    const powerTile = tile("Power now", "–", "W", "Live");
    fill(liveCard, powerTile);
    openLive(hm, (m) => {
      powerTile.querySelector(".value").firstChild.textContent = fmtNum(m.power, 0);
      fill(liveCard, powerTile, tile("Used today", fmtNum(m.accumulatedConsumption, 2), "kWh", "Live"),
        tile("Cost today", fmtMoney(m.accumulatedCost, m.currency), null, "Live"));
    });
  } else {
    api(`/v1/homes/${hm.id}/energy?` + new URLSearchParams({ type: "consumption", resolution: "hourly", from: today.from, to: today.to }))
      .then((e) => fillToday(e.totals.energy, e.totals.cost, e.currency, "From hourly data; may lag a few hours"))
      .catch((e) => fill(liveCard, errorCard(e)));
  }

  // Device tiles with each device's first capability.
  api(`/v1/homes/${hm.id}/devices`).then(async (d) => {
    if (!d.available) { fill(devCard, h("div", { class: "card muted small" }, "Connect the Tibber Data API in Settings to see chargers, vehicles, batteries and thermostats.")); return; }
    if (!d.devices.length) return;
    const grid = h("div", { class: "grid" });
    fill(devCard, h("h2", {}, "Devices"), grid);
    for (const dev of d.devices) {
      const t = h("a", { class: "card tile device-tile", href: `#/devices/${dev.id}` }, h("div", { class: "label" }, dev.name), h("div", { class: "value" }, "…"), h("div", { class: "sub" }, [dev.brand, dev.model].filter(Boolean).join(" ")));
      grid.append(t);
      api(`/v1/homes/${hm.id}/devices/${dev.id}`).then((full) => {
        const c = full.capabilities[0];
        fill(t.querySelector(".value"), c ? capValue(c) : "–");
        if (c) t.querySelector(".label").textContent = `${dev.name} · ${c.description || c.id}`;
      }).catch(() => { t.querySelector(".value").textContent = "–"; });
    }
  }).catch((e) => fill(devCard, errorCard(e)));
  await prices;
};

function noHomes() {
  fill(view(), h("h1", {}, "Welcome"), h("div", { class: "card" },
    h("p", {}, "No homes yet. Paste your Tibber personal access token and/or connect the Data API in Settings."),
    h("a", { href: "#/settings" }, "Open Settings")));
}

function startOfLocalDay(tz, offsetDays = 0) {
  // Find local midnight in `tz` without a date library: walk the formatter.
  const now = new Date();
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now).map((p) => [p.type, p.value]));
  const localMidnight = (y, m, d) => {
    const guess = Date.UTC(y, m - 1, d);
    for (const off of [-14, -13, -12, -11, -10, -9, -8, -7, -6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]) {
      const t = new Date(guess - off * 3600e3);
      const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).formatToParts(t).map((q) => [q.type, q.value]));
      if (+p.year === y && +p.month === m && +p.day === d && +p.hour === 0 && +p.minute === 0) return t;
    }
    return new Date(guess);
  };
  const base = new Date(Date.UTC(+parts.year, +parts.month - 1, +parts.day + offsetDays));
  const from = localMidnight(base.getUTCFullYear(), base.getUTCMonth() + 1, base.getUTCDate());
  const nb = new Date(base.getTime() + 86400e3);
  const to = localMidnight(nb.getUTCFullYear(), nb.getUTCMonth() + 1, nb.getUTCDate());
  return { from: toRfc3339(from), to: toRfc3339(to), fromDate: from, toDate: to };
}
const toRfc3339 = (d) => d.toISOString().replace(/\.\d{3}Z$/, "Z");

function priceChart(card, p, resolution) {
  const series = [...p.today, ...p.tomorrow];
  const canvas = h("canvas", { role: "img", "aria-label": "Price per time slot, coloured by price level" });
  const legend = h("div", { class: "legend" }, ...Object.keys(LEVEL_LABEL).map(levelBadge));
  const table = h("details", {}, h("summary", {}, "Show as table"), h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Starts"), h("th", { class: "num" }, `Price (${p.currency || ""}/kWh)`), h("th", {}, "Level"))),
    h("tbody", {}, ...series.map((x) => h("tr", {}, h("td", {}, fmtTime(x.startsAt, p.timeZone, { weekday: "short", hour: "2-digit", minute: "2-digit" })), h("td", { class: "num" }, fmtNum(x.total, 3)), h("td", {}, LEVEL_LABEL[x.level] || "–")))))));
  const wrap = h("div", { class: "chart tall" }, canvas);
  card.append(wrap, legend);
  if (!p.tomorrow.length && p.tomorrowExpectedAfter) {
    const due = new Date(p.tomorrowExpectedAfter).getTime() > Date.now() + 60e3;
    card.append(h("p", { class: "small muted" }, due
      ? `Tomorrow's prices are expected after ${fmtTime(p.tomorrowExpectedAfter, p.timeZone)}.`
      : "Tomorrow's prices are not published yet; checking again every 15 minutes."));
  }
  card.append(table);
  const fallback = cssVar("--series-1");
  const colors = series.map((x) => (x.level ? cssVar(LEVEL_VAR[x.level]) : fallback));
  const currentIdx = series.findIndex((x) => x === p.current || (p.current && x.startsAt === p.current.startsAt));
  chart(canvas, {
    type: "bar",
    data: {
      labels: series.map((x) => fmtTime(x.startsAt, p.timeZone, { weekday: "short", hour: "2-digit", minute: "2-digit" })),
      datasets: [{ data: series.map((x) => x.total), backgroundColor: colors, borderRadius: 2, categoryPercentage: 1, barPercentage: resolution === "hourly" ? 0.85 : 0.95,
        borderColor: series.map((_, i) => (i === currentIdx ? cssVar("--text-primary") : "transparent")), borderWidth: series.map((_, i) => (i === currentIdx ? 2 : 0)) }],
    },
    options: baseOptions({
      plugins: { legend: { display: false }, tooltip: { displayColors: false, callbacks: {
        label: (ctx) => `${fmtMoney(ctx.parsed.y, p.currency, 3)}/kWh${series[ctx.dataIndex].level ? " · " + LEVEL_LABEL[series[ctx.dataIndex].level] : ""}`,
      } } },
      scales: { ...baseOptions().scales, y: { ...baseOptions().scales.y, beginAtZero: false, title: { display: true, text: `${p.currency || ""}/kWh`, color: cssVar("--text-secondary") } } },
    }),
  });
}

// ------------------------------------------------------------------ live
function openLive(hm, onMeasurement, onStatus) {
  const es = new EventSource(`/v1/homes/${hm.id}/live`);
  es.addEventListener("measurement", (e) => onMeasurement(JSON.parse(e.data)));
  es.addEventListener("status", (e) => {
    const s = JSON.parse(e.data);
    if (onStatus) onStatus(s);
    if (s.state === "unavailable") es.close();
  });
  onLeave(() => es.close());
  return es;
}

screens.live = async () => {
  const hm = home();
  if (!hm) return noHomes();
  const status = h("p", { class: "small muted" }, "Connecting…");
  const hero = h("div", { class: "grid" });
  const stats = h("div", { class: "grid" });
  const phases = h("div", { class: "card", hidden: true });
  const canvas = h("canvas", { role: "img", "aria-label": "Power over the last minutes" });
  const sparkCard = h("div", { class: "card" }, h("h2", {}, "Power, last 10 minutes"), h("div", { class: "chart" }, canvas));
  fill(view(), h("h1", {}, "Live"), status, hero, sparkCard, stats, phases);
  const points = [];
  const c = chart(canvas, {
    type: "line",
    data: { labels: [], datasets: [{ data: [], borderColor: cssVar("--series-1"), borderWidth: 2, pointRadius: 0, tension: 0.2 }] },
    options: baseOptions({ plugins: { legend: { display: false }, tooltip: { displayColors: false, callbacks: { label: (ctx) => `${fmtNum(ctx.parsed.y, 0)} W` } } } }),
  });
  openLive(hm, (m) => {
    const cur = m.currency;
    fill(hero, tile("Power now", fmtNum(m.power, 0), "W", m.timestamp ? `at ${fmtTime(m.timestamp, hm.timeZone, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : null, "hero"),
      m.powerProduction ? tile("Producing", fmtNum(m.powerProduction, 0), "W") : null);
    fill(stats, 
      tile("Average power today", fmtNum(m.averagePower, 0), "W", `Min ${fmtNum(m.minPower, 0)} W · max ${fmtNum(m.maxPower, 0)} W`),
      tile("Consumed today", fmtNum(m.accumulatedConsumption, 2), "kWh", `This hour ${fmtNum(m.accumulatedConsumptionLastHour, 2)} kWh`),
      tile("Cost today", fmtMoney(m.accumulatedCost, cur)),
      m.accumulatedProduction ? tile("Produced today", fmtNum(m.accumulatedProduction, 2), "kWh", `Reward ${fmtMoney(m.accumulatedReward, cur)}`) : null);
    const rows = [1, 2, 3].filter((i) => m[`voltageL${i}`] != null || m[`currentL${i}`] != null)
      .map((i) => h("tr", {}, h("td", {}, `L${i}`), h("td", { class: "num" }, fmtNum(m[`voltageL${i}`], 1)), h("td", { class: "num" }, fmtNum(m[`currentL${i}`], 1))));
    phases.hidden = !rows.length;
    if (rows.length) fill(phases, h("h2", {}, "Per phase"), h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Phase"), h("th", { class: "num" }, "Voltage (V)"), h("th", { class: "num" }, "Current (A)"))), h("tbody", {}, ...rows)));
    if (m.power != null) {
      points.push([m.timestamp ? new Date(m.timestamp) : new Date(), m.power]);
      const cutoff = Date.now() - 10 * 60e3;
      while (points.length && points[0][0].getTime() < cutoff) points.shift();
      c.data.labels = points.map(([t]) => fmtTime(t.toISOString(), hm.timeZone, { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
      c.data.datasets[0].data = points.map(([, v]) => v);
      c.update("none");
    }
  }, (s) => {
    status.textContent = { connecting: "Connecting…", connected: `Live (${s.source === "graphql" ? "Tibber API" : "Data API"})`,
      reconnecting: `Reconnecting in ${s.retryInSeconds ?? "?"} s…`, unavailable: `Live data unavailable: ${s.reason || "no Pulse or Watty"}` }[s.state] || s.state;
  });
};

// ------------------------------------------------------------------ prices
screens.prices = async (params) => {
  const hm = home();
  if (!hm) return noHomes();
  const resolution = params.get("resolution") === "hourly" ? "hourly" : "quarter_hourly";
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Resolution" },
    ...[["quarter_hourly", "15 min"], ["hourly", "Hourly"]].map(([v, l]) => h("button", { "aria-pressed": String(v === resolution), onclick: () => { location.hash = `#/prices?resolution=${v}`; } }, l)));
  const top = h("div", { class: "grid" });
  const chartCard = h("div", { class: "card" }, h("h2", {}, "Today and tomorrow"));
  const finder = h("div", { class: "card" });
  fill(view(), h("h1", {}, "Prices"), h("div", { class: "row" }, seg), top, chartCard, finder);
  let p;
  try { p = await api(`/v1/homes/${hm.id}/prices?resolution=${resolution}`); }
  catch (e) { view().append(errorCard(e)); return; }
  showStale(p.meta);
  if (!p.available) { fill(top, h("div", { class: "card" }, "No price data for this account")); chartCard.hidden = finder.hidden = true; return; }
  const todayVals = p.today.map((x) => x.total).filter((v) => v != null);
  fill(top, 
    tile("Now", p.current ? fmtMoney(p.current.total, p.currency, 3) : "–", "/kWh", p.current ? levelBadge(p.current.level) : null, "hero"),
    tile("Today min / max", `${fmtMoney(Math.min(...todayVals), p.currency, 2)} / ${fmtMoney(Math.max(...todayVals), p.currency, 2)}`),
    tile("Today average", fmtMoney(todayVals.reduce((a, b) => a + b, 0) / (todayVals.length || 1), p.currency, 3)),
    p.tomorrow.length ? tile("Tomorrow average", fmtMoney(p.tomorrow.reduce((a, b) => a + (b.total || 0), 0) / p.tomorrow.length, p.currency, 3)) : null);
  priceChart(chartCard, p, resolution);

  // Poll for tomorrow's prices after the expected publish time instead of spinning.
  if (!p.tomorrow.length && p.tomorrowExpectedAfter) {
    const wait = Math.max(new Date(p.tomorrowExpectedAfter).getTime() - Date.now(), 0) + 60e3;
    const t = setTimeout(() => route(), Math.min(wait, 15 * 60e3));
    onLeave(() => clearTimeout(t));
  }

  // Cheapest window finder
  const dur = h("select", { "aria-label": "Duration" }, ...[1, 2, 3, 4, 6].map((n) => h("option", { value: n * 60, selected: n === 2 }, `${n} h`)));
  const when = h("select", { "aria-label": "When" }, h("option", { value: "tonight" }, "tonight (18–07)"), h("option", { value: "next" }, "from now"), h("option", { value: "tomorrow" }, "tomorrow"));
  const result = h("p", { "aria-live": "polite" });
  const find = async () => {
    const qs = new URLSearchParams({ duration: dur.value });
    const today = startOfLocalDay(hm.timeZone), tomorrow = startOfLocalDay(hm.timeZone, 1);
    if (when.value === "tonight") {
      qs.set("from", toRfc3339(new Date(Math.max(today.fromDate.getTime() + 18 * 3600e3, Date.now()))));
      qs.set("to", toRfc3339(new Date(tomorrow.fromDate.getTime() + 7 * 3600e3)));
    } else if (when.value === "tomorrow") {
      qs.set("from", tomorrow.from); qs.set("to", tomorrow.to);
    }
    try {
      const r = await api(`/v1/homes/${hm.id}/prices/cheapest-window?${qs}`);
      result.textContent = r.window
        ? `Cheapest: ${fmtTime(r.window.start, hm.timeZone, { weekday: "short", hour: "2-digit", minute: "2-digit" })}–${fmtTime(r.window.end, hm.timeZone)} at ${fmtMoney(r.window.averageTotal, r.window.currency, 3)}/kWh on average.`
        : "Not enough known prices for that window yet.";
    } catch (e) { result.textContent = e.message; }
  };
  fill(finder, h("h2", {}, "Cheapest window"), h("div", { class: "row" }, "Cheapest", dur, when, h("button", { class: "primary", onclick: find }, "Find")), result);
  find();
};

// ------------------------------------------------------------------ analysis
screens.analysis = async (params) => {
  const hm = home();
  if (!hm) return noHomes();
  const type = params.get("type") === "production" ? "production" : "consumption";
  const period = ["day", "week", "month", "year"].includes(params.get("period")) ? params.get("period") : "month";
  const offset = Math.min(0, parseInt(params.get("offset") || "0", 10) || 0);
  const go = (patch) => { const q = new URLSearchParams({ type, period, offset: String(offset), ...patch }); location.hash = `#/analysis?${q}`; };
  const segType = h("div", { class: "seg", role: "group", "aria-label": "Type" },
    ...[["consumption", "Consumption"], ["production", "Production"]].map(([v, l]) => h("button", { "aria-pressed": String(v === type), onclick: () => go({ type: v }) }, l)));
  const segPeriod = h("div", { class: "seg", role: "group", "aria-label": "Period" },
    ...[["day", "Day"], ["week", "Week"], ["month", "Month"], ["year", "Year"]].map(([v, l]) => h("button", { "aria-pressed": String(v === period), onclick: () => go({ period: v, offset: "0" }) }, l)));
  const nav = h("div", { class: "row" }, h("button", { onclick: () => go({ offset: String(offset - 1) }), "aria-label": "Previous period" }, "‹"),
    h("span", { id: "range-label" }, "…"), h("button", { onclick: () => go({ offset: String(offset + 1) }), disabled: offset >= 0, "aria-label": "Next period" }, "›"));
  const tiles = h("div", { class: "grid" });
  const c1 = h("canvas", { role: "img", "aria-label": "Energy per bucket" });
  const c2 = h("canvas", { role: "img", "aria-label": "Cost per bucket" });
  const tableHost = h("div", {});
  fill(view(), h("h1", {}, "Analysis"), h("div", { class: "row" }, segType, segPeriod), nav, tiles,
    h("div", { class: "card" }, h("h2", {}, type === "consumption" ? "Energy used (kWh)" : "Energy produced (kWh)"), h("div", { class: "chart" }, c1)),
    h("div", { class: "card" }, h("h2", {}, type === "consumption" ? "Cost" : "Earnings"), h("div", { class: "chart" }, c2)),
    tableHost,
    h("p", { class: "small muted" }, "Comparison with other households is not offered: that data is not available through Tibber's public APIs."));

  const { from, to, resolution, label } = periodRange(hm.timeZone, period, offset);
  $("#range-label").textContent = label;
  let e;
  try { e = await api(`/v1/homes/${hm.id}/energy?` + new URLSearchParams({ type, resolution, from, to })); }
  catch (err) { fill(tiles, errorCard(err)); return; }
  showStale(e.meta);
  const pct = (a, b) => (a == null || b == null || b === 0 ? null : ((a - b) / Math.abs(b)) * 100);
  const deltaSub = (a, b, invert = false) => {
    const d = pct(a, b);
    if (d === null) return "No previous data";
    const up = d > 0;
    return h("span", { class: (up !== invert) ? "delta-up" : "delta-down" }, `${up ? "▲" : "▼"} ${fmtNum(Math.abs(d), 0)} % vs previous`);
  };
  const t = e.totals, pv = e.previous;
  fill(tiles, 
    tile(type === "consumption" ? "Used" : "Produced", fmtNum(t.energy, 1), "kWh", deltaSub(t.energy, pv.energy, type === "production")),
    tile(type === "consumption" ? "Cost" : "Earned", fmtMoney(t.cost, e.currency), null, deltaSub(t.cost, pv.cost, type === "production")),
    type === "consumption" ? tile("Price performance", t.pricePerformance ? `${t.pricePerformance.percent > 0 ? "+" : ""}${fmtNum(t.pricePerformance.percent, 1)} %` : "–", null,
      t.pricePerformance ? `Paid ${fmtMoney(t.pricePerformance.averagePaid, e.currency, 3)}/kWh vs average ${fmtMoney(t.pricePerformance.averagePrice, e.currency, 3)}/kWh` : "Needs hourly data") : null);
  const labels = e.buckets.map((b) => bucketLabel(b.from, resolution, hm.timeZone));
  const barColor = cssVar(type === "consumption" ? "--series-1" : "--series-2");
  chart(c1, { type: "bar", data: { labels, datasets: [{ data: e.buckets.map((b) => b.energy), backgroundColor: barColor, borderRadius: 4 }] },
    options: baseOptions({ plugins: { legend: { display: false }, tooltip: { displayColors: false, callbacks: { label: (x) => `${fmtNum(x.parsed.y, 2)} kWh` } } } }) });
  chart(c2, { type: "bar", data: { labels, datasets: [{ data: e.buckets.map((b) => b.cost), backgroundColor: barColor, borderRadius: 4 }] },
    options: baseOptions({ plugins: { legend: { display: false }, tooltip: { displayColors: false, callbacks: { label: (x) => fmtMoney(x.parsed.y, e.currency) } } } }) });
  fill(tableHost, h("details", { class: "card" }, h("summary", {}, "Show as table"), h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Period"), h("th", { class: "num" }, "kWh"), h("th", { class: "num" }, type === "consumption" ? "Cost" : "Earned"), h("th", { class: "num" }, "Unit price"))),
    h("tbody", {}, ...e.buckets.map((b, i) => h("tr", {}, h("td", {}, labels[i]), h("td", { class: "num" }, fmtNum(b.energy, 2)), h("td", { class: "num" }, fmtMoney(b.cost, e.currency)), h("td", { class: "num" }, fmtNum(b.unitPrice, 3)))))))));
};

function periodRange(tz, period, offset) {
  const today = startOfLocalDay(tz);
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" }).formatToParts(today.fromDate).map((p) => [p.type, p.value]));
  const dayOffset = (n) => startOfLocalDay(tz, n);
  const y = +parts.year, m = +parts.month, d = +parts.day;
  const daysBetween = (a, b) => Math.round((Date.UTC(...b) - Date.UTC(...a)) / 86400e3);
  if (period === "day") {
    const r = dayOffset(offset);
    return { from: r.from, to: r.to, resolution: "hourly", label: fmtTime(r.from, tz, { weekday: "short", day: "numeric", month: "short", year: "numeric" }) };
  }
  if (period === "week") {
    const wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(parts.weekday);
    const a = dayOffset(-wd + 7 * offset), b = dayOffset(-wd + 7 * offset + 7);
    return { from: a.from, to: b.from, resolution: "daily", label: `Week of ${fmtTime(a.from, tz, { day: "numeric", month: "short", year: "numeric" })}` };
  }
  if (period === "month") {
    const mm = m - 1 + offset, ys = y + Math.floor(mm / 12), ms = ((mm % 12) + 12) % 12;
    const a = dayOffset(daysBetween([y, m - 1, d], [ys, ms, 1])), b = dayOffset(daysBetween([y, m - 1, d], [ys, ms + 1, 1]));
    return { from: a.from, to: b.from, resolution: "daily", label: fmtTime(a.from, tz, { month: "long", year: "numeric" }) };
  }
  const a = dayOffset(daysBetween([y, m - 1, d], [y + offset, 0, 1])), b = dayOffset(daysBetween([y, m - 1, d], [y + offset + 1, 0, 1]));
  return { from: a.from, to: b.from, resolution: "monthly", label: String(y + offset) };
}

function bucketLabel(iso, resolution, tz) {
  if (resolution === "hourly") return fmtTime(iso, tz);
  if (resolution === "daily") return fmtTime(iso, tz, { day: "numeric", month: "short" });
  if (resolution === "monthly") return fmtTime(iso, tz, { month: "short" });
  return fmtTime(iso, tz, { year: "numeric" });
}

// ------------------------------------------------------------------ devices
function capValue(c) {
  if (c.kind === "enum") return h("span", {}, String(c.value ?? "–"));
  if (c.kind === "number" || c.kind === "integer") return h("span", {}, fmtNum(c.value, 2), c.unit ? h("span", { class: "unit" }, c.unit) : null);
  return h("span", {}, c.value === null ? "–" : String(c.value));
}

screens.devices = async (params, deviceId) => {
  const hm = home();
  if (!hm) return noHomes();
  if (deviceId) return deviceScreen(hm, deviceId);
  let d;
  try { d = await api(`/v1/homes/${hm.id}/devices`); }
  catch (e) { fill(view(), h("h1", {}, "Devices"), errorCard(e)); return; }
  showStale(d.meta);
  if (!d.available) {
    fill(view(), h("h1", {}, "Devices"), h("div", { class: "card" }, "Connect the Tibber Data API in Settings and choose the device scopes you want.", h("br"), h("a", { href: "#/settings" }, "Open Settings")));
    return;
  }
  fill(view(), h("h1", {}, "Devices"),
    d.devices.length ? h("div", { class: "grid" }, ...d.devices.map((x) => h("a", { class: "card tile device-tile", href: `#/devices/${x.id}` },
      h("div", { class: "value" }, x.name), h("div", { class: "sub" }, [x.brand, x.model].filter(Boolean).join(" "))))) :
      h("div", { class: "card" }, "No devices. Devices only appear for the categories (scopes) you granted. Vehicles appear under every home."),
    h("p", { class: "small muted" }, "The Data API does not say whether a device is a car, charger, battery or thermostat, so every device is shown with the capabilities Tibber reports for it."));
};

async function deviceScreen(hm, deviceId) {
  const load = async () => {
    const dev = await api(`/v1/homes/${hm.id}/devices/${deviceId}`);
    showStale(dev.meta);
    const caps = h("div", { class: "grid" }, ...dev.capabilities.map((c) => h("div", { class: "card tile" },
      h("div", { class: "label" }, c.description || c.id), h("div", { class: "value" }, capValue(c)),
      c.kind === "enum" && c.availableValues ? h("div", { class: "sub" }, `One of: ${c.availableValues.join(", ")}`) : null)));
    const attrs = dev.attributes.length ? h("div", { class: "card" }, h("h2", {}, "Attributes"), h("table", {}, h("tbody", {},
      ...dev.attributes.map((a) => h("tr", {}, h("td", {}, a.description || a.id), h("td", {}, a.value === null ? "–" : String(a.value))))))) : null;
    const control = h("div", { class: "card small muted" }, h("b", {}, "Control: "), "not available. The official APIs are read-only for devices (no charger, battery-mode or thermostat changes).");
    const historyCard = h("div", { class: "card" });
    fill(view(), h("h1", {}, dev.name), h("p", { class: "muted small" }, [dev.brand, dev.model].filter(Boolean).join(" "), dev.lastSeen ? ` · last seen ${new Date(dev.lastSeen).toLocaleString()}` : ""),
      dev.capabilities.length ? caps : h("div", { class: "card muted" }, "This device reports no capabilities."), historyCard, attrs, control);
    if (dev.historyResolutions.length) historyView(hm, dev, historyCard); else fill(historyCard, h("p", { class: "muted small" }, "No history is available for this device."));
  };
  try { await load(); } catch (e) { fill(view(), errorCard(e)); return; }
  const t = setInterval(() => load().catch(() => {}), 5 * 60e3);
  onLeave(() => clearInterval(t));
}

function historyView(hm, dev, card) {
  const prefer = ["hour", "day", "quarterHour", "month"].find((r) => dev.historyResolutions.includes(r));
  const resSel = h("select", { "aria-label": "Resolution" }, ...dev.historyResolutions.map((r) => h("option", { value: r, selected: r === prefer }, r)));
  const keySel = h("select", { "aria-label": "Value" });
  const canvas = h("canvas", { role: "img", "aria-label": "Device history" });
  const prevBtn = h("button", {}, "‹ Older"), nextBtn = h("button", {}, "Newer ›");
  fill(card, h("h2", {}, "History"), h("div", { class: "row" }, resSel, keySel, prevBtn, nextBtn), h("div", { class: "chart" }, canvas));
  let page = null, c = null;
  const flatten = (obj, prefix = "", out = {}) => {
    for (const [k, v] of Object.entries(obj || {})) {
      const key = prefix ? `${prefix}.${k}` : k;
      if (typeof v === "number") out[key] = v;
      else if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, key, out);
    }
    return out;
  };
  const draw = () => {
    const rows = page.items.map((i) => [i.time, flatten(i.data)]);
    const keys = [...new Set(rows.flatMap(([, d]) => Object.keys(d)))];
    if (!keys.includes(keySel.value)) fill(keySel, ...keys.map((k) => h("option", { value: k }, k)));
    const key = keySel.value;
    c?.destroy();
    c = chart(canvas, { type: "line", data: { labels: rows.map(([t]) => fmtTime(t, hm.timeZone, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })),
      datasets: [{ data: rows.map(([, d]) => d[key] ?? null), borderColor: cssVar("--series-1"), borderWidth: 2, pointRadius: 0, spanGaps: false }] },
      options: baseOptions({ scales: { ...baseOptions().scales, y: { ...baseOptions().scales.y, beginAtZero: false } } }) });
    prevBtn.disabled = !page.prev; nextBtn.disabled = !page.next;
  };
  const fetchPage = async (qs) => {
    try { page = await api(`/v1/homes/${hm.id}/devices/${dev.id}/history?${qs}`); draw(); }
    catch (e) { card.append(errorCard(e)); }
  };
  const start = () => {
    const back = { quarterHour: 1, hour: 2, day: 31, month: 365 }[resSel.value] || 2;
    fetchPage(new URLSearchParams({ resolution: resSel.value, since: toRfc3339(new Date(Date.now() - back * 86400e3)) }));
  };
  resSel.addEventListener("change", start);
  keySel.addEventListener("change", draw);
  prevBtn.addEventListener("click", () => page?.prev && fetchPage(new URLSearchParams({ cursor: page.prev })));
  nextBtn.addEventListener("click", () => page?.next && fetchPage(new URLSearchParams({ cursor: page.next })));
  start();
}

// ------------------------------------------------------------------ settings
const NOT_AVAILABLE = [
  ["Battery mode, charger and thermostat control, away mode", "The official APIs are read-only for devices."],
  ["Checklist, offer cards, articles, store, messages, invites", "App-only content; no public API."],
  ["Power-up catalogue, instalment summary", "App-only content; no public API."],
  ["Notification categories, weather widget, feature flags", "App-only content; no public API."],
  ["Push notifications", "The official sendPushNotification only reaches the Tibber app, so this app does not build its own push on it."],
  ["Comparison with other households", "Not exposed by any public API."],
];

screens.settings = async (params) => {
  const s = await api("/v1/settings");
  const oauthResult = params.get("oauth");
  const scopes = h("div", { class: "scopes" }, ...s.scopes.map((x) => h("label", {},
    h("input", { type: "checkbox", value: x.name, checked: x.required || x.granted, disabled: x.required }),
    h("span", {}, x.required ? x.name : `${x.label} `, x.required ? null : h("code", { class: "small muted" }, x.name)),
    x.granted ? h("span", { class: "badge" }, "granted") : null)));
  const msg = h("p", { role: "status" });
  const connect = async () => {
    const chosen = [...scopes.querySelectorAll("input:not([disabled]):checked")].map((i) => i.value);
    try { const r = await api("/v1/settings/oauth", { json: { scopes: chosen } }); location.assign(r.authorizeUrl); }
    catch (e) { msg.textContent = e.message; }
  };
  const disconnect = async () => { await api("/v1/settings/oauth", { method: "DELETE" }); route(); };
  const tokenInput = h("input", { type: "password", autocomplete: "off", placeholder: "Paste personal access token", "aria-label": "Personal access token" });
  const saveToken = async () => {
    try { await api("/v1/settings/personal-token", { method: "PUT", json: { token: tokenInput.value.trim() } }); tokenInput.value = ""; await loadHomes(); route(); }
    catch (e) { msg.textContent = e.message; }
  };
  const removeToken = async () => { await api("/v1/settings/personal-token", { method: "DELETE" }); route(); };
  const logout = async () => { await api("/v1/session", { method: "DELETE" }); location.hash = "#/login"; };

  fill(view(), h("h1", {}, "Settings"),
    oauthResult ? h("div", { class: "card " + (oauthResult === "ok" ? "" : "error") }, oauthResult === "ok" ? "Tibber Data API connected." : "Connecting to Tibber failed. Check the client id, secret and redirect URI, then try again.") : null,
    h("div", { class: "card" }, h("h2", {}, "Prices, energy and live data (Tibber API)"),
      h("p", { class: "small muted" }, "Create a personal access token at developer.tibber.com/settings/access-token and paste it here. It is stored encrypted on the server and never sent back to the browser."),
      s.personalTokenSet ? h("p", {}, "A token is stored. ", h("button", { class: "danger", onclick: removeToken }, "Remove token")) :
        h("div", { class: "row" }, tokenInput, h("button", { class: "primary", onclick: saveToken }, "Save token"))),
    h("div", { class: "card" }, h("h2", {}, "Devices and Pulse stream (Tibber Data API)"),
      !s.oauthConfigured ? h("p", { class: "error" }, "OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET and OAUTH_REDIRECT_URI are not set on the server.") : null,
      h("p", { class: "small muted" }, "Choose the device categories to share. Tibber shows a consent screen; changing scopes later means connecting again."),
      scopes,
      h("div", { class: "row" }, h("button", { class: "primary", onclick: connect, disabled: !s.oauthConfigured }, s.dataApiConnected ? "Reconnect with these scopes" : "Connect Tibber"),
        s.dataApiConnected ? h("button", { class: "danger", onclick: disconnect }, "Disconnect") : null),
      s.dataApiConnected ? h("p", { class: "small muted" }, "Disconnect deletes the stored tokens. Tibber documents no revocation endpoint; to revoke fully, delete the client at data-api.tibber.com/clients/manage.") : null),
    h("div", { class: "card" }, h("h2", {}, "Not available through the official APIs"),
      ...NOT_AVAILABLE.map(([what, why]) => h("div", { class: "na" }, h("span", { class: "badge" }, "Not available"), h("div", {}, h("b", {}, what), h("div", { class: "small muted" }, why))))),
    msg,
    h("div", { class: "row" }, h("button", { onclick: logout }, "Sign out")));
};

// ------------------------------------------------------------------ router
async function route() {
  for (const fn of cleanups.splice(0)) { try { fn(); } catch { /* ignore */ } }
  while (charts.length) charts.pop().destroy();
  $("#stale").hidden = true;
  const [path, query] = (location.hash.slice(2) || "home").split("?");
  const [name, arg] = path.split("/");
  const params = new URLSearchParams(query || "");
  if (!(name in screens)) { location.hash = "#/home"; return; }
  document.querySelectorAll(".tabs a").forEach((a) => {
    if (a.dataset.tab === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (name === "login") return screens.login();
  try {
    if (!state.homes.length || name === "home") await loadHomes();
    $(".tabs").hidden = false;
    showStale(state.homesMeta);
    await screens[name](params, arg);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return;
    fill(view(), errorCard(e));
  }
}

window.addEventListener("hashchange", route);
// Refresh on return after a while away (device state "on screen open"); the server cache keeps this cheap.
let hiddenAt = 0;
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") hiddenAt = Date.now();
  else if (hiddenAt && Date.now() - hiddenAt > 60e3 && !location.hash.startsWith("#/login")) route();
});
route();
