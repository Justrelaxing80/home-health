/* Home Health - single-page frontend (no build step). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const PROFILE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const LB_PER_KG = 2.2046226218;
const RANGES = [[30, "30 days"], [90, "90 days"], [365, "1 year"]];

const state = { profiles: [], charts: [], poll: null };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail || res.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new Error(msg);
  }
  return data;
}

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

const initial = (name) => esc((name || "?").trim().charAt(0).toUpperCase());
const avatar = (p, cls = "") => `<span class="avatar ${cls}" style="background:${esc(p.color)}" aria-hidden="true">${initial(p.name)}</span>`;

// ---------------- formatting ----------------
const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1, minimumFractionDigits: 1 });
const weightUnit = (p) => (p.units === "metric" ? "kg" : "lb");
const toWeight = (p, kg) => (kg == null ? null : p.units === "metric" ? kg : kg * LB_PER_KG);
const distUnit = (p) => (p.units === "metric" ? "km" : "mi");
const toDist = (p, m) => (m == null ? null : p.units === "metric" ? m / 1000 : m / 1609.344);
function hm(sec) {
  if (sec == null) return "–";
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return `${h}h ${String(m).padStart(2, "0")}m`;
}
const dayTs = (d) => Date.parse(d + "T12:00:00") / 1000;
const shortDate = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const longDate = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

// ---------------- routing ----------------
window.addEventListener("hashchange", route);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => route());
document.addEventListener("click", (e) => {
  const menu = $(".menu.open");
  if (menu && !e.target.closest(".switcher")) menu.classList.remove("open");
});

async function route() {
  clearInterval(state.poll);
  state.charts.forEach((c) => c.destroy());
  state.charts = [];
  try {
    state.profiles = await api("/api/profiles");
  } catch (e) {
    $("#view").innerHTML = `<p class="err">Can't reach the server: ${esc(e.message)}</p>`;
    return;
  }
  const hash = location.hash || "";
  let m;
  if (!hash || hash === "#") {
    const last = store.get("lastProfile");
    if (last && state.profiles.some((p) => String(p.id) === last)) { location.hash = `#/p/${last}`; return; }
    return renderPicker();
  }
  if (hash === "#/") return renderPicker();
  if ((m = hash.match(/^#\/p\/(\d+)\/settings$/))) return renderSettings(+m[1]);
  if ((m = hash.match(/^#\/p\/(\d+)$/))) return renderDashboard(+m[1]);
  location.hash = "#/";
}

function setTopbar(profile, { onSettings = false } = {}) {
  const el = $("#topbar-actions");
  if (!profile) { el.innerHTML = ""; return; }
  const others = state.profiles.map((p) => `
    <a href="#/p/${p.id}${onSettings ? "/settings" : ""}" class="${p.id === profile.id ? "current" : ""}">${avatar(p)} ${esc(p.name)}</a>`).join("");
  el.innerHTML = `
    ${onSettings
      ? `<a class="btn" href="#/p/${profile.id}">Dashboard</a>`
      : `<button class="btn" id="sync-btn" title="Pull new data from linked accounts">Sync now</button>
         <a class="btn" href="#/p/${profile.id}/settings" title="Settings">Settings</a>`}
    <div class="switcher">
      <button aria-haspopup="true" aria-expanded="false" title="Switch profile">${avatar(profile)}<span class="name">${esc(profile.name)}</span> ▾</button>
      <div class="menu" role="menu">
        ${others}
        <hr>
        <button data-act="add">＋ Add profile</button>
        <a href="#/">All profiles</a>
      </div>
    </div>`;
  const btn = $(".switcher > button", el), menu = $(".menu", el);
  btn.onclick = () => { const open = menu.classList.toggle("open"); btn.setAttribute("aria-expanded", open); };
  $('[data-act="add"]', el).onclick = () => { menu.classList.remove("open"); openProfileDialog(); };
  const sync = $("#sync-btn");
  if (sync) sync.onclick = () => syncNow(profile.id, sync);
}

async function syncNow(pid, btn) {
  try {
    const r = await api(`/api/profiles/${pid}/sync`, { method: "POST" });
    if (!r.queued.length) { toast("Nothing to sync — link Garmin or Renpho in Settings."); return; }
    toast(`Syncing ${r.queued.join(" + ")}…`);
    if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
    pollUntilIdle(pid, () => route());
  } catch (e) { toast(e.message); }
}

function pollUntilIdle(pid, done) {
  clearInterval(state.poll);
  state.poll = setInterval(async () => {
    try {
      const conns = await api(`/api/profiles/${pid}/connections`);
      if (!conns.some((c) => c.status === "syncing")) { clearInterval(state.poll); done(); }
    } catch { clearInterval(state.poll); }
  }, 3000);
}

// ---------------- profile dialog ----------------
function openProfileDialog() {
  const dlg = $("#profile-dialog");
  const color = PROFILE_COLORS[state.profiles.length % PROFILE_COLORS.length];
  $("#pf-name").value = ""; $("#pf-units").value = "imperial"; $("#pf-goal").value = 10000;
  $("#pf-err").hidden = true;
  renderSwatches($("#pf-colors"), color);
  dlg.showModal();
  $("#profile-form").onsubmit = async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    try {
      const p = await api("/api/profiles", { method: "POST", body: {
        name: $("#pf-name").value, color: $("#pf-colors").dataset.value,
        units: $("#pf-units").value, step_goal: +$("#pf-goal").value || 10000 } });
      dlg.close();
      location.hash = `#/p/${p.id}/settings`;
    } catch (err) { $("#pf-err").textContent = err.message; $("#pf-err").hidden = false; }
  };
}

function renderSwatches(el, selected) {
  el.dataset.value = selected;
  el.innerHTML = PROFILE_COLORS.map((c) => `<button type="button" style="background:${c}" aria-label="Color ${c}" aria-pressed="${c === selected}" data-c="${c}"></button>`).join("");
  el.querySelectorAll("button").forEach((b) => b.onclick = () => {
    el.dataset.value = b.dataset.c;
    el.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  });
}

// ---------------- picker ----------------
function renderPicker() {
  setTopbar(null);
  $("#view").innerHTML = `
    <section class="picker">
      <h1>Who's checking in?</h1>
      <p class="sub">Pick a profile to see their dashboard.</p>
      <div class="people">
        ${state.profiles.map((p) => `
          <a class="person" href="#/p/${p.id}">${avatar(p, "lg")}<span>${esc(p.name)}</span>
            <small>${p.connections.length ? esc(p.connections.map((c) => c[0].toUpperCase() + c.slice(1)).join(" · ")) : "No accounts linked"}</small></a>`).join("")}
        <button class="person" id="add-person"><span class="avatar lg add">＋</span><span>Add profile</span><small>&nbsp;</small></button>
      </div>
      ${state.profiles.some((p) => p.name === "Demo") ? "" : `<button class="btn" id="demo-btn">Add a demo profile with sample data</button>`}
    </section>`;
  $("#add-person").onclick = openProfileDialog;
  const demo = $("#demo-btn");
  if (demo) demo.onclick = async () => {
    demo.disabled = true; demo.textContent = "Creating…";
    const p = await api("/api/demo", { method: "POST" });
    location.hash = `#/p/${p.id}`;
  };
}

// ---------------- dashboard ----------------
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function theme() {
  return { s: [css("--s1"), css("--s2"), css("--s3"), css("--s4")], ink: css("--ink"), ink2: css("--ink-2"),
    muted: css("--muted"), grid: css("--grid"), axis: css("--axis"), surface: css("--surface"), border: css("--border") };
}

const crosshair = {
  id: "crosshair",
  afterDraw(chart) {
    const a = chart.tooltip && chart.tooltip.getActiveElements();
    if (!a || !a.length || chart.config.type !== "line") return;
    const x = a[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
    ctx.save(); ctx.strokeStyle = theme().axis; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
  },
};

function baseOptions(t, { linearX = false, stacked = false, yFmt = (v) => nf0.format(v), tipFmt, titleFmt, points } = {}) {
  // Pin a time axis to the data so ticks don't pad past the first/last reading.
  const xRange = linearX && points && points.length ? { min: points[0].x, max: points[points.length - 1].x } : {};
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2, borderColor: t.axis, borderWidth: 1,
        padding: 10, boxPadding: 4, usePointStyle: true,
        callbacks: {
          title: (items) => (titleFmt ? titleFmt(items) : items[0].label),
          label: (item) => ` ${item.dataset.label}: ${tipFmt ? tipFmt(item.parsed.y) : yFmt(item.parsed.y)}`,
        },
      },
    },
    scales: {
      x: {
        type: linearX ? "linear" : "category", stacked, ...xRange,
        grid: { display: false }, border: { color: t.axis },
        ticks: { color: t.muted, maxTicksLimit: 6, maxRotation: 0, autoSkipPadding: 12,
          ...(linearX ? { callback: (v) => shortDate(v) } : {}) },
      },
      y: {
        stacked, grid: { color: t.grid }, border: { display: false },
        ticks: { color: t.muted, maxTicksLimit: 5, callback: (v) => yFmt(v) },
      },
    },
  };
}

function lineDataset(t, label, color, points) {
  return { label, data: points, borderColor: color, backgroundColor: color, borderWidth: 2, tension: 0.25,
    pointRadius: points.length < 40 ? 3 : 0, pointHoverRadius: 5, pointBackgroundColor: color,
    pointBorderColor: t.surface, pointBorderWidth: 2, spanGaps: true };
}

function chartCard({ id, title, subtitle, legend, table }) {
  return `
    <section class="chart-card" id="card-${id}">
      <header>
        <div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div>
        ${table ? `<button class="linkbtn" data-toggle="${id}">Table</button>` : ""}
      </header>
      ${legend ? `<div class="legend">${legend.map(([n, c, dash]) => `<span><i style="background:${dash ? "transparent" : c};${dash ? `border-top:2px dashed ${c};height:0;border-radius:0` : ""}"></i>${esc(n)}</span>`).join("")}</div>` : ""}
      <div class="chart-box"><canvas id="chart-${id}" role="img" aria-label="${esc(title)} chart"></canvas></div>
      <div class="data-table" hidden></div>
    </section>`;
}

function wireTable(id, table) {
  const card = $(`#card-${id}`);
  const btn = $(`[data-toggle="${id}"]`, card);
  if (!btn) return;
  btn.onclick = () => {
    const box = $(".chart-box", card), tbl = $(".data-table", card), showTable = tbl.hidden;
    if (showTable && !tbl.innerHTML) {
      tbl.innerHTML = `<table><thead><tr>${table.cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${table.rows.slice().reverse().map((r) => `<tr>${r.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }
    tbl.hidden = !showTable; box.hidden = showTable;
    btn.textContent = showTable ? "Chart" : "Table";
  };
}

function emptyCard(title, msg) {
  return `<section class="chart-card"><header><div><h2>${esc(title)}</h2></div></header><div class="empty-chart">${msg}</div></section>`;
}

function lastWith(rows, key) {
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i][key] != null) return rows[i];
  return null;
}
function avg(rows, key, n = 7) {
  const vals = rows.slice(-n).map((r) => r[key]).filter((v) => v != null);
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}

async function renderDashboard(pid) {
  const days = +(store.get("range") || 90);
  let d;
  try { d = await api(`/api/profiles/${pid}/dashboard?days=${days}`); }
  catch { location.hash = "#/"; return; }
  store.set("lastProfile", String(pid));
  const p = d.profile, t = theme();
  setTopbar(p);

  const conns = d.connections;
  const lastSync = conns.map((c) => c.last_sync).filter(Boolean).sort().pop();
  const problems = conns.filter((c) => c.status === "error" || c.status === "needs_relink");
  const hasData = d.daily.length || d.body.length || d.latest_body;

  // ---- tiles ----
  const tiles = [];
  const lb = d.latest_body;
  if (lb && lb.weight_kg) {
    const first = d.body[0];
    const delta = first && first.measured_at !== lb.measured_at ? toWeight(p, lb.weight_kg) - toWeight(p, first.weight_kg) : null;
    tiles.push({ label: "Weight", value: nf1.format(toWeight(p, lb.weight_kg)), unit: weightUnit(p),
      meta: delta != null ? `${delta > 0 ? "▲" : delta < 0 ? "▼" : ""} ${nf1.format(Math.abs(delta))} ${weightUnit(p)} in ${days} days` : `Weighed ${shortDate(lb.measured_at)}` });
  }
  if (lb && lb.body_fat != null) tiles.push({ label: "Body fat", value: nf1.format(lb.body_fat), unit: "%", meta: lb.visceral_fat != null ? `Visceral fat ${nf1.format(lb.visceral_fat)}` : "" });
  const st = lastWith(d.daily, "steps");
  if (st) {
    const pct = Math.min(100, (st.steps / p.step_goal) * 100);
    tiles.push({ label: "Steps", value: nf0.format(st.steps), unit: "", meta: `${dayLabel(st.date)} · 7-day avg ${nf0.format(avg(d.daily, "steps") || 0)}`, bar: pct });
  }
  const sl = lastWith(d.daily, "sleep_s");
  if (sl) tiles.push({ label: "Sleep", value: hm(sl.sleep_s).replace(/m$/, ""), unit: "m", meta: sl.sleep_score != null ? `Score ${sl.sleep_score} · ${dayLabel(sl.date)}` : dayLabel(sl.date) });
  const rhr = lastWith(d.daily, "resting_hr");
  if (rhr) tiles.push({ label: "Resting HR", value: rhr.resting_hr, unit: "bpm", meta: `7-day avg ${nf0.format(avg(d.daily, "resting_hr"))}` });
  const hrv = lastWith(d.daily, "hrv_avg");
  if (hrv) tiles.push({ label: "HRV", value: hrv.hrv_avg, unit: "ms", meta: `7-day avg ${nf0.format(avg(d.daily, "hrv_avg"))}` });
  const bb = lastWith(d.daily, "body_battery_high");
  if (bb) tiles.push({ label: "Body Battery", value: bb.body_battery_high, unit: "", meta: `Low ${bb.body_battery_low ?? "–"} · ${dayLabel(bb.date)}` });
  const stress = lastWith(d.daily, "avg_stress");
  if (stress) tiles.push({ label: "Stress", value: stress.avg_stress, unit: "avg", meta: dayLabel(stress.date) });

  // ---- view ----
  $("#view").innerHTML = `
    ${!conns.length ? `<div class="banner"><span>No accounts linked yet. Connect Garmin and/or Renpho to start filling this dashboard.</span><a class="btn primary" href="#/p/${pid}/settings">Connect accounts</a></div>` : ""}
    ${problems.map((c) => `<div class="banner ${c.status === "needs_relink" ? "warn" : "bad"}"><span><b>${c.provider === "garmin" ? "Garmin" : "Renpho"}:</b> ${esc(c.last_error || "Sync problem")}</span><a class="btn small" href="#/p/${pid}/settings">Fix in Settings</a></div>`).join("")}
    <div class="dash-head">
      <div><h1>${esc(p.name)}</h1><p class="sub" style="margin:0">${conns.length ? `Last synced ${ago(lastSync)}` : "Not connected"}</p></div>
      <div class="seg" role="group" aria-label="Date range">
        ${RANGES.map(([n, l]) => `<button data-days="${n}" aria-pressed="${n === days}">${l}</button>`).join("")}
      </div>
    </div>
    ${tiles.length ? `<div class="tiles">${tiles.map((x) => `
      <div class="tile"><div class="label">${esc(x.label)}</div>
        <div class="value">${esc(x.value)}${x.unit ? `<small>${esc(x.unit)}</small>` : ""}</div>
        ${x.bar != null ? `<div class="bar" title="${nf0.format(x.bar)}% of ${nf0.format(p.step_goal)} goal"><span style="width:${x.bar}%"></span></div>` : ""}
        <div class="meta">${esc(x.meta || "")}</div></div>`).join("")}</div>` : ""}
    <div class="charts" id="charts"></div>
    <section class="card activities" id="activities"></section>`;

  document.querySelectorAll(".seg button").forEach((b) => b.onclick = () => { store.set("range", b.dataset.days); route(); });

  if (!hasData) {
    $("#charts").innerHTML = emptyCard("No data yet", conns.length ? "Linked accounts are syncing — this can take a minute for the first run." : "Link an account in Settings, or import a Renpho CSV.");
    $("#activities").remove();
    if (conns.some((c) => c.status === "syncing")) pollUntilIdle(pid, () => route());
    return;
  }

  const charts = $("#charts");
  const wu = weightUnit(p);
  const specs = [];

  // Weight
  const wPts = d.body.filter((b) => b.weight_kg != null).map((b) => ({ x: b.measured_at, y: toWeight(p, b.weight_kg) }));
  specs.push(wPts.length ? {
    id: "weight", title: `Weight (${wu})`, subtitle: `${wPts.length} weigh-ins`,
    table: { cols: ["Date", `Weight (${wu})`], rows: wPts.map((q) => [longDate(q.x), nf1.format(q.y)]) },
    build: () => ({ type: "line", data: { datasets: [lineDataset(t, "Weight", t.s[0], wPts)] },
      options: baseOptions(t, { linearX: true, points: wPts, yFmt: (v) => nf0.format(v), tipFmt: (v) => `${nf1.format(v)} ${wu}`, titleFmt: (it) => longDate(it[0].parsed.x) }) }),
  } : { empty: emptyCard("Weight", "No scale readings in this range.") });

  // Body fat
  const fPts = d.body.filter((b) => b.body_fat != null).map((b) => ({ x: b.measured_at, y: b.body_fat }));
  if (fPts.length) specs.push({
    id: "fat", title: "Body fat (%)", subtitle: "From your Renpho scale",
    table: { cols: ["Date", "Body fat (%)"], rows: fPts.map((q) => [longDate(q.x), nf1.format(q.y)]) },
    build: () => ({ type: "line", data: { datasets: [lineDataset(t, "Body fat", t.s[0], fPts)] },
      options: baseOptions(t, { linearX: true, points: fPts, yFmt: (v) => nf0.format(v) + "%", tipFmt: (v) => nf1.format(v) + "%", titleFmt: (it) => longDate(it[0].parsed.x) }) }),
  });

  // Steps (bars + goal reference)
  const stepRows = d.daily.filter((r) => r.steps != null).slice(-Math.min(days, 60));
  if (stepRows.length) specs.push({
    id: "steps", title: "Daily steps", subtitle: days > 60 ? "Last 60 days" : `Last ${days} days`,
    legend: [["Steps", t.s[0]], [`Goal (${nf0.format(p.step_goal)})`, t.muted, true]],
    table: { cols: ["Date", "Steps"], rows: stepRows.map((r) => [longDate(dayTs(r.date)), nf0.format(r.steps)]) },
    build: () => ({ type: "bar", data: { labels: stepRows.map((r) => shortDate(dayTs(r.date))), datasets: [
      { type: "line", label: "Goal", data: stepRows.map(() => p.step_goal), borderColor: t.muted, borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 0 },
      { label: "Steps", data: stepRows.map((r) => r.steps), backgroundColor: t.s[0], borderRadius: 4, borderSkipped: "start", maxBarThickness: 18, categoryPercentage: 0.85, barPercentage: 0.9 },
    ] }, options: baseOptions(t, { yFmt: (v) => (v >= 1000 ? nf0.format(v / 1000) + "k" : nf0.format(v)), tipFmt: (v) => nf0.format(v), titleFmt: (it) => longDate(dayTs(stepRows[it[0].dataIndex].date)) }) }),
  });

  // Sleep stages (stacked)
  const sleepRows = d.daily.filter((r) => r.sleep_s != null).slice(-30);
  if (sleepRows.length) {
    const stages = [["Deep", "deep_s"], ["Light", "light_s"], ["REM", "rem_s"], ["Awake", "awake_s"]];
    specs.push({
      id: "sleep", title: "Sleep stages (hours)", subtitle: `Last ${sleepRows.length} nights`,
      legend: stages.map(([n], i) => [n, t.s[i]]),
      table: { cols: ["Night", "Total", ...stages.map(([n]) => n), "Score"],
        rows: sleepRows.map((r) => [longDate(dayTs(r.date)), hm(r.sleep_s), ...stages.map(([, k]) => hm(r[k])), r.sleep_score ?? "–"]) },
      build: () => ({ type: "bar", data: { labels: sleepRows.map((r) => shortDate(dayTs(r.date))),
        datasets: stages.map(([n, k], i) => ({ label: n, data: sleepRows.map((r) => (r[k] ?? 0) / 3600), backgroundColor: t.s[i],
          borderColor: t.surface, borderWidth: { top: 2 }, borderSkipped: "start", maxBarThickness: 18 })) },
        options: baseOptions(t, { stacked: true, yFmt: (v) => nf0.format(v) + "h", tipFmt: (v) => hm(v * 3600), titleFmt: (it) => longDate(dayTs(sleepRows[it[0].dataIndex].date)) }) }),
    });
  }

  // Resting HR
  const rPts = d.daily.filter((r) => r.resting_hr != null).map((r) => ({ x: dayTs(r.date), y: r.resting_hr }));
  if (rPts.length) specs.push({
    id: "rhr", title: "Resting heart rate (bpm)", subtitle: "Lower usually means better recovery",
    table: { cols: ["Date", "Resting HR (bpm)"], rows: rPts.map((q) => [longDate(q.x), q.y]) },
    build: () => ({ type: "line", data: { datasets: [lineDataset(t, "Resting HR", t.s[0], rPts)] },
      options: baseOptions(t, { linearX: true, points: rPts, tipFmt: (v) => `${v} bpm`, titleFmt: (it) => longDate(it[0].parsed.x) }) }),
  });

  // HRV
  const hPts = d.daily.filter((r) => r.hrv_avg != null).map((r) => ({ x: dayTs(r.date), y: r.hrv_avg }));
  if (hPts.length) specs.push({
    id: "hrv", title: "Overnight HRV (ms)", subtitle: "Nightly average",
    table: { cols: ["Date", "HRV (ms)"], rows: hPts.map((q) => [longDate(q.x), q.y]) },
    build: () => ({ type: "line", data: { datasets: [lineDataset(t, "HRV", t.s[0], hPts)] },
      options: baseOptions(t, { linearX: true, points: hPts, tipFmt: (v) => `${v} ms`, titleFmt: (it) => longDate(it[0].parsed.x) }) }),
  });

  charts.innerHTML = specs.map((s) => s.empty || chartCard(s)).join("");
  for (const s of specs) {
    if (s.empty) continue;
    state.charts.push(new Chart($(`#chart-${s.id}`), { ...s.build(), plugins: [crosshair] }));
    wireTable(s.id, s.table);
  }

  // Activities
  const acts = d.activities;
  const actEl = $("#activities");
  if (!acts.length) { actEl.remove(); }
  else {
    const du = distUnit(p);
    actEl.innerHTML = `<h2 style="margin-bottom:8px">Recent activities</h2><div class="scroll"><table>
      <thead><tr><th>Activity</th><th>Date</th><th>Duration</th><th>Distance</th><th>Avg HR</th><th>Calories</th></tr></thead>
      <tbody>${acts.map((a) => `<tr>
        <td>${esc(a.name || prettyType(a.type))}</td>
        <td>${esc(new Date(a.start_time.replace(" ", "T")).toLocaleDateString(undefined, { month: "short", day: "numeric" }))}</td>
        <td>${a.duration_s ? hm(a.duration_s) : "–"}</td>
        <td>${a.distance_m ? `${nf1.format(toDist(p, a.distance_m))} ${du}` : "–"}</td>
        <td>${a.avg_hr ? nf0.format(a.avg_hr) : "–"}</td>
        <td>${a.calories ? nf0.format(a.calories) : "–"}</td></tr>`).join("")}</tbody></table></div>`;
  }

  if (conns.some((c) => c.status === "syncing")) {
    const b = $("#sync-btn"); if (b) { b.disabled = true; b.textContent = "Syncing…"; }
    pollUntilIdle(pid, () => route());
  }
}

function dayLabel(iso) {
  const today = new Date(); const d = new Date(iso + "T12:00:00");
  const diff = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  return diff === 0 ? "Today" : diff === 1 ? "Yesterday" : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const prettyType = (t) => (t || "Activity").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

// ---------------- settings ----------------
const STATUS_TEXT = { linked: "Connected", syncing: "Syncing…", error: "Sync error", needs_relink: "Needs re-link" };

async function renderSettings(pid) {
  const p = state.profiles.find((x) => x.id === pid);
  if (!p) { location.hash = "#/"; return; }
  const conns = await api(`/api/profiles/${pid}/connections`);
  const conn = (prov) => conns.find((c) => c.provider === prov);
  setTopbar(p, { onSettings: true });

  const providerCard = (prov, title, blurb, extra = "") => {
    const c = conn(prov);
    return `<section class="card" id="card-${prov}">
      <div class="row" style="justify-content:space-between;margin-bottom:6px"><h2>${title}</h2>
        ${c ? `<span class="status ${c.status}">${STATUS_TEXT[c.status] || esc(c.status)}</span>` : `<span class="status">Not linked</span>`}</div>
      <p class="note">${blurb}</p>
      ${c ? `<p class="note">Account: <b>${esc(c.email)}</b> · Last sync: ${ago(c.last_sync)}</p>
             ${c.last_error ? `<p class="err">${esc(c.last_error)}</p>` : ""}
             <div class="row" style="margin:10px 0 14px">
               <button class="btn small" data-sync="${prov}">Sync now</button>
               <button class="btn small" data-relink="${prov}">Re-link</button>
               <button class="btn small danger" data-unlink="${prov}">Unlink</button></div>` : ""}
      <form class="link-form" data-prov="${prov}" ${c ? "hidden" : ""} autocomplete="off">
        <div class="field"><label>${title} email</label><input name="email" type="email" required value="${esc(c ? c.email : "")}"></div>
        <div class="field"><label>${title} password</label><input name="password" type="password" required autocomplete="new-password"></div>
        <button class="btn primary">Link ${title}</button>
        <p class="err" hidden></p>
      </form>
      <form class="mfa-form" data-prov="${prov}" hidden autocomplete="off">
        <p class="note">Garmin sent a verification code to your email/phone. Enter it here:</p>
        <div class="field"><label>Verification code</label><input name="code" inputmode="numeric" required maxlength="12"></div>
        <button class="btn primary">Verify</button>
        <p class="err" hidden></p>
      </form>
      ${extra}
    </section>`;
  };

  $("#view").innerHTML = `
    <h1>${esc(p.name)} · Settings</h1>
    <p class="sub">Link this person's accounts. Each profile uses its own Garmin and Renpho logins.</p>
    <div class="settings">
      <section class="card">
        <h2 style="margin-bottom:12px">Profile</h2>
        <form id="edit-profile">
          <div class="field"><label for="ep-name">Name</label><input id="ep-name" required maxlength="40" value="${esc(p.name)}"></div>
          <div class="field"><label>Color</label><div class="swatches" id="ep-colors"></div></div>
          <div class="field"><label for="ep-units">Units</label><select id="ep-units">
            <option value="imperial" ${p.units === "imperial" ? "selected" : ""}>Imperial (lb, mi)</option>
            <option value="metric" ${p.units === "metric" ? "selected" : ""}>Metric (kg, km)</option></select></div>
          <div class="field"><label for="ep-goal">Daily step goal</label><input id="ep-goal" type="number" min="1000" max="100000" step="500" value="${p.step_goal}"></div>
          <div class="row"><button class="btn primary">Save</button><button type="button" class="btn danger" id="del-profile">Delete profile</button></div>
          <p class="err" hidden></p>
        </form>
      </section>
      ${providerCard("garmin", "Garmin", "Steps, sleep, resting heart rate, HRV, Body Battery, stress and activities. Uses Garmin Connect's unofficial login, as most self-hosted tools do. Your password and session are stored encrypted on this server.")}
      ${providerCard("renpho", "Renpho", "Weight and body composition from your Renpho scale via Renpho's cloud (unofficial API). If cloud sync stops working, use CSV import below.", `
        <hr style="border:0;border-top:1px solid var(--border);margin:16px 0">
        <h2 style="font-size:15px;margin-bottom:4px">Import Renpho CSV</h2>
        <p class="note">In the Renpho app: <i>Me → Data export</i> (or <i>Settings → Export data</i>), then upload the file here. Re-importing the same file is safe.</p>
        <div class="row"><input type="file" id="csv-file" accept=".csv,text/csv"><button class="btn small" id="csv-btn">Import</button></div>
        <p id="csv-msg" hidden></p>`)}
    </div>`;

  renderSwatches($("#ep-colors"), p.color);
  $("#edit-profile").onsubmit = async (e) => {
    e.preventDefault();
    const err = $(".err", e.target);
    try {
      await api(`/api/profiles/${pid}`, { method: "PUT", body: { name: $("#ep-name").value, color: $("#ep-colors").dataset.value, units: $("#ep-units").value, step_goal: +$("#ep-goal").value || 10000 } });
      toast("Profile saved"); route();
    } catch (x) { err.textContent = x.message; err.hidden = false; }
  };
  $("#del-profile").onclick = async () => {
    if (!confirm(`Delete ${p.name} and all of their data? This can't be undone.`)) return;
    await api(`/api/profiles/${pid}`, { method: "DELETE" });
    if (store.get("lastProfile") === String(pid)) store.set("lastProfile", "");
    location.hash = "#/";
  };

  document.querySelectorAll(".link-form").forEach((f) => f.onsubmit = async (e) => {
    e.preventDefault();
    const prov = f.dataset.prov, btn = $("button", f), err = $(".err", f);
    err.hidden = true; btn.disabled = true; btn.textContent = "Signing in…";
    try {
      const r = await api(`/api/profiles/${pid}/${prov}/link`, { method: "POST", body: { email: f.email.value, password: f.password.value } });
      if (r.status === "needs_mfa") {
        f.hidden = true;
        const m = $(`.mfa-form[data-prov="${prov}"]`); m.hidden = false; m.code.focus();
        return;
      }
      toast(`${prov === "garmin" ? "Garmin" : "Renpho"} linked — first sync started`);
      route();
    } catch (x) {
      err.textContent = x.message; err.hidden = false;
    } finally { btn.disabled = false; btn.textContent = `Link ${prov === "garmin" ? "Garmin" : "Renpho"}`; }
  });
  document.querySelectorAll(".mfa-form").forEach((f) => f.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("button", f), err = $(".err", f);
    btn.disabled = true; err.hidden = true;
    try {
      await api(`/api/profiles/${pid}/garmin/mfa`, { method: "POST", body: { code: f.code.value } });
      toast("Garmin linked — first sync started"); route();
    } catch (x) { err.textContent = x.message; err.hidden = false; btn.disabled = false; }
  });
  document.querySelectorAll("[data-relink]").forEach((b) => b.onclick = () => {
    const f = $(`.link-form[data-prov="${b.dataset.relink}"]`); f.hidden = !f.hidden; if (!f.hidden) f.password.focus();
  });
  document.querySelectorAll("[data-unlink]").forEach((b) => b.onclick = async () => {
    if (!confirm("Unlink this account? Data already synced stays on the dashboard.")) return;
    await api(`/api/profiles/${pid}/${b.dataset.unlink}`, { method: "DELETE" });
    route();
  });
  document.querySelectorAll("[data-sync]").forEach((b) => b.onclick = async () => {
    await api(`/api/profiles/${pid}/sync`, { method: "POST" });
    toast("Sync started"); route();
  });
  if (conns.some((c) => c.status === "syncing")) pollUntilIdle(pid, () => route());

  $("#csv-btn").onclick = async () => {
    const file = $("#csv-file").files[0], msg = $("#csv-msg");
    if (!file) { toast("Choose a CSV file first"); return; }
    const fd = new FormData(); fd.append("file", file);
    msg.hidden = false; msg.className = "note"; msg.textContent = "Importing…";
    try {
      const r = await api(`/api/profiles/${pid}/import/renpho-csv`, { method: "POST", body: fd });
      msg.className = "ok";
      msg.textContent = `Imported ${r.imported} readings${r.skipped ? ` (${r.skipped} rows skipped)` : ""}. Columns found: ${r.columns.join(", ")}.`;
    } catch (x) { msg.className = "err"; msg.textContent = x.message; }
  };
}

route();
