/* Home Health - peptide tracker: today's doses, vials, log, site rotation, calculator, reminders.
   Uses helpers defined in app.js ($, esc, api, toast, state, navTabs, setTopbar, ...). */
"use strict";

const pad2 = (n) => String(n).padStart(2, "0");
const localDay = (d = new Date()) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const localStamp = (d = new Date()) => `${localDay(d)}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const addDays = (day, n) => { const d = new Date(day + "T12:00:00"); d.setDate(d.getDate() + n); return localDay(d); };
const timeOf = (stamp) => new Date(stamp).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const hhmmLabel = (t) => timeOf(`2000-01-01T${t}`);
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const ROUTES = ["SubQ", "IM", "Oral", "Nasal", "Topical", "Other"];
const nf2 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

function fmtDose(mcg, unit) {
  if (mcg == null) return "–";
  return unit === "mg" ? `${nf2.format(mcg / 1000)} mg` : `${nf0.format(mcg)} mcg`;
}
const fmtUnits = (u) => (u == null ? "" : `${nf1.format(u)} units`);

function scheduleText(p) {
  if (p.freq === "as_needed") return "As needed";
  const times = (p.times || "").split(",").filter(Boolean).map(hhmmLabel).join(", ");
  const when = p.freq === "weekdays"
    ? p.weekdays.split(",").filter(Boolean).map((w) => WEEKDAYS[+w]).join(", ")
    : p.freq === "interval" ? (p.interval_days === 1 ? "Daily" : `Every ${p.interval_days} days`) : "Daily";
  return `${when} at ${times}`;
}

function modal(html, cls = "") {
  const d = document.createElement("dialog");
  if (cls) d.className = cls;
  d.innerHTML = html;
  document.body.appendChild(d);
  d.addEventListener("close", () => d.remove());
  d.showModal();
  return d;
}

// ---------------- page ----------------
async function renderPeptides(pid) {
  const p = state.profiles.find((x) => x.id === pid);
  if (!p) { location.hash = "#/"; return; }
  store.set("lastProfile", String(pid));
  setTopbar(p, { section: "peptides" });
  const today = localDay();
  if (!state.pepDay || state.pepPid !== pid) { state.pepDay = today; state.pepPid = pid; }
  const day = state.pepDay;

  let list, view, hist, notify;
  try {
    [list, view, hist, notify] = await Promise.all([
      api(`/api/profiles/${pid}/peptides`),
      api(`/api/profiles/${pid}/peptides/day?day=${day}`),
      api(`/api/profiles/${pid}/peptides/history?days=30`),
      api(`/api/profiles/${pid}/notify`),
    ]);
  } catch (e) {
    $("#view").innerHTML = `${navTabs(pid, "peptides")}<p class="err">${esc(e.message)}</p>`;
    return;
  }
  const peps = list.peptides;
  const byId = Object.fromEntries(peps.map((x) => [x.id, x]));
  const now = localStamp();

  // ---- tiles ----
  const todayView = day === today ? view : null;
  const tiles = [];
  if (todayView) {
    const taken = todayView.doses.filter((x) => x.status === "taken").length;
    tiles.push({ label: "Today", value: todayView.doses.length ? `${taken}/${todayView.doses.length}` : "–", unit: todayView.doses.length ? "taken" : "",
      meta: todayView.doses.length ? `${todayView.doses.filter((x) => x.status === "pending" && x.scheduled_for < now).length} overdue` : "No doses scheduled" });
  }
  tiles.push({ label: "30-day adherence", value: hist.adherence == null ? "–" : hist.adherence, unit: hist.adherence == null ? "" : "%",
    meta: hist.due ? `${hist.taken} of ${hist.due} scheduled doses logged` : "No scheduled doses yet" });
  if (hist.streak) tiles.push({ label: "Streak", value: hist.streak.current, unit: hist.streak.current === 1 ? "day" : "days",
    meta: hist.streak.best > hist.streak.current ? `Best ${hist.streak.best} · every scheduled dose taken` : "Days with every scheduled dose taken" });
  const next = peps.filter((x) => x.next_dose && x.active).sort((a, b) => a.next_dose.localeCompare(b.next_dose))[0];
  tiles.push({ label: "Next dose", value: next ? timeOf(next.next_dose) : "–", unit: "",
    meta: next ? `${next.name} · ${dayLabel(next.next_dose.slice(0, 10))}` : "Nothing scheduled" });
  tiles.push({ label: "Next injection site", value: hist.suggested_site, unit: "", small: true, meta: "Least recently used" });

  $("#view").innerHTML = `
    ${navTabs(pid, "peptides")}
    <div class="dash-head">
      <div><h1>Peptides</h1><p class="sub" style="margin:0">Schedule, dose log and vials for ${esc(p.name)}</p></div>
      <div class="row">
        ${peps.length ? `<button class="btn" id="pep-log-any">Log a dose</button>` : ""}
        <button class="btn primary" id="pep-add">＋ Add peptide</button>
      </div>
    </div>
    <div class="tiles">${tiles.map((x) => `
      <div class="tile"><div class="label">${esc(x.label)}</div>
        <div class="value${x.small ? " sm" : ""}">${esc(x.value)}${x.unit ? `<small>${esc(x.unit)}</small>` : ""}</div>
        <div class="meta">${esc(x.meta)}</div></div>`).join("")}</div>
    <div class="pep-grid">
      <section class="card" id="pep-day">
        <div class="day-nav">
          <button class="btn small" data-day="-1" aria-label="Previous day">‹</button>
          <h2>${esc(dayLabel(day))} <span class="note">${esc(new Date(day + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" }))}</span></h2>
          <button class="btn small" data-day="1" aria-label="Next day">›</button>
          ${day !== today ? `<button class="btn small" data-day="0">Today</button>` : ""}
        </div>
        ${doseList(view, day, today, now)}
      </section>
      <section class="card" id="pep-vials">
        <h2 style="margin-bottom:10px">My peptides</h2>
        ${peps.length ? peps.map((x) => vialCard(x)).join("") : `<div class="empty-chart" style="height:auto;padding:28px 0">No peptides yet.<br><button class="btn primary" style="margin-top:10px" id="pep-add-2">Add your first peptide</button></div>`}
      </section>
      <section class="card" id="pep-history">
        <h2 style="margin-bottom:8px">Dose log <span class="note">· last 30 days</span></h2>
        ${historyTable(hist.logs)}
      </section>
      <section class="card">
        <h2 style="margin-bottom:4px">Injection site rotation</h2>
        <p class="note" style="margin-top:0">Rotate sites to avoid irritation. The least recently used site is suggested.</p>
        <div class="sites">${hist.sites.map((s) => `
          <div class="site${s.site === hist.suggested_site ? " next" : ""}"><b>${esc(s.site)}</b>
            <span>${s.site === hist.suggested_site ? "Next · " : ""}${s.last ? esc(dayAgo(s.last)) : "Not used yet"}</span></div>`).join("")}</div>
      </section>
      <section class="card" id="pep-calc">${calculatorHtml()}</section>
      <section class="card" id="pep-notify">${notifyHtml(notify)}</section>
    </div>
    <p class="note disclaimer">For personal tracking only. This isn't medical advice; follow your prescriber's directions.</p>`;

  // ---- wiring ----
  document.querySelectorAll("[data-day]").forEach((b) => b.onclick = () => {
    const n = +b.dataset.day;
    state.pepDay = n === 0 ? today : addDays(day, n);
    route();
  });
  const add = () => openPeptideDialog(pid, null);
  $("#pep-add").onclick = add;
  if ($("#pep-add-2")) $("#pep-add-2").onclick = add;
  if ($("#pep-log-any")) $("#pep-log-any").onclick = () => openLogDialog(pid, { peps, sites: list.sites, suggested: hist.suggested_site });

  document.querySelectorAll("[data-log]").forEach((b) => b.onclick = () => {
    const [id, slot] = b.dataset.log.split("|");
    openLogDialog(pid, { peps, sites: list.sites, suggested: hist.suggested_site, peptide: byId[id], scheduled_for: slot });
  });
  document.querySelectorAll("[data-skip]").forEach((b) => b.onclick = async () => {
    const [id, slot] = b.dataset.skip.split("|");
    await api(`/api/profiles/${pid}/peptide-logs`, { method: "POST", body: { peptide_id: +id, status: "skipped", taken_at: localStamp(), scheduled_for: slot } });
    toast("Marked as skipped"); route();
  });
  document.querySelectorAll("[data-undo]").forEach((b) => b.onclick = async () => {
    await api(`/api/profiles/${pid}/peptide-logs/${b.dataset.undo}`, { method: "DELETE" });
    toast("Removed from log"); route();
  });
  document.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => openPeptideDialog(pid, byId[b.dataset.edit]));
  document.querySelectorAll("[data-logpep]").forEach((b) => b.onclick = () =>
    openLogDialog(pid, { peps, sites: list.sites, suggested: hist.suggested_site, peptide: byId[b.dataset.logpep] }));
  document.querySelectorAll("[data-vial]").forEach((b) => b.onclick = async () => {
    const x = byId[b.dataset.vial];
    if (!confirm(`Start a new vial of ${x.name} today? The remaining amount resets to a full ${nf2.format(x.vial_mg)} mg.`)) return;
    await api(`/api/profiles/${pid}/peptides/${x.id}/new-vial`, { method: "POST" });
    toast("New vial started"); route();
  });
  wireCalculator($("#pep-calc"));
  wireNotify(pid, notify);
}

function dayAgo(stamp) {
  const days = Math.round((new Date(localDay() + "T12:00:00") - new Date(stamp.slice(0, 10) + "T12:00:00")) / 86400000);
  return days <= 0 ? "Today" : days === 1 ? "Yesterday" : `${days} days ago`;
}

function doseList(view, day, today, now) {
  const rows = view.doses.map((x) => {
    let label, cls, actions;
    const slot = `${x.peptide_id}|${x.scheduled_for}`;
    if (x.status === "taken") {
      cls = "taken"; label = `✓ Taken ${timeOf(x.log.taken_at)}${x.log.site ? ` · ${esc(x.log.site)}` : ""}`;
      actions = `<button class="linkbtn" data-undo="${x.log.id}">Undo</button>`;
    } else if (x.status === "skipped") {
      cls = "skipped"; label = "– Skipped";
      actions = `<button class="linkbtn" data-undo="${x.log.id}">Undo</button>`;
    } else {
      const late = x.scheduled_for < now;
      cls = late ? (day === today ? "overdue" : "missed") : "upcoming";
      label = late ? (day === today ? "! Overdue" : "Missed") : "Upcoming";
      actions = `<button class="btn small primary" data-log="${slot}">Log</button><button class="btn small" data-skip="${slot}">Skip</button>`;
    }
    return `<li class="dose ${cls}">
      <span class="pdot" style="background:${esc(x.color)}"></span>
      <div class="dose-main"><b>${esc(x.name)}</b>
        <span class="note">${fmtDose(x.dose_mcg, x.dose_unit)}${x.units != null ? ` · ${fmtUnits(x.units)}` : ""} · ${esc(x.route)}</span></div>
      <div class="dose-time">${timeOf(x.scheduled_for)}</div>
      <div class="dose-status"><span class="chip ${cls}">${label}</span><div class="dose-actions">${actions}</div></div>
    </li>`;
  });
  const extra = view.unscheduled.map((lg) => `<li class="dose ${lg.status}">
      <span class="pdot" style="background:var(--muted)"></span>
      <div class="dose-main"><b>${esc(lg.peptide_name)}</b><span class="note">${fmtDose(lg.dose_mcg, "mcg")}${lg.units != null ? ` · ${fmtUnits(lg.units)}` : ""} · extra dose</span></div>
      <div class="dose-time">${timeOf(lg.taken_at)}</div>
      <div class="dose-status"><span class="chip taken">✓ Taken${lg.site ? ` · ${esc(lg.site)}` : ""}</span><div class="dose-actions"><button class="linkbtn" data-undo="${lg.id}">Undo</button></div></div>
    </li>`);
  if (!rows.length && !extra.length) return `<p class="note" style="padding:18px 0;text-align:center">No doses scheduled for this day.</p>`;
  return `<ul class="doses">${rows.join("")}${extra.join("")}</ul>`;
}

function vialCard(x) {
  const total = x.vial_mg ? x.vial_mg * 1000 : null;
  const pct = total && x.remaining_mcg != null ? Math.max(0, Math.min(100, (x.remaining_mcg / total) * 100)) : null;
  const expired = x.expires_on && x.expires_on < localDay();
  const daysToExp = x.expires_on ? Math.round((new Date(x.expires_on + "T12:00:00") - new Date(localDay() + "T12:00:00")) / 86400000) : null;
  return `<div class="vial${x.active ? "" : " paused"}">
    <div class="row" style="justify-content:space-between">
      <b class="row" style="gap:8px"><span class="pdot" style="background:${esc(x.color)}"></span>${esc(x.name)}</b>
      <span class="chip ${x.active ? "taken" : ""}">${x.active ? "Active" : "Paused"}</span>
    </div>
    <div class="note">${fmtDose(x.dose_mcg, x.dose_unit)} · ${esc(scheduleText(x))} · ${esc(x.route)}</div>
    ${x.concentration ? `<div class="note">${nf2.format(x.vial_mg)} mg vial + ${nf2.format(x.water_ml)} mL water = ${nf0.format(x.concentration)} mcg/mL · draw <b>${fmtUnits(x.dose_units)}</b> per dose</div>` : x.vial_mg ? "" : `<div class="note">No vial details. Add them to track what's left.</div>`}
    ${pct != null ? `<div class="bar" title="${nf0.format(pct)}% left"><span style="width:${pct}%;${pct < 15 ? "background:var(--bad)" : ""}"></span></div>
      <div class="note">${nf2.format(x.remaining_mcg / 1000)} of ${nf2.format(x.vial_mg)} mg left${x.doses_left != null ? ` · about ${x.doses_left} dose${x.doses_left === 1 ? "" : "s"}` : ""}${x.expires_on ? ` · ${expired ? `<span class="err-inline">expired ${esc(dayAgo(x.expires_on))}</span>` : daysToExp <= 3 ? `<span class="err-inline">discard in ${daysToExp} day${daysToExp === 1 ? "" : "s"}</span>` : `discard after ${new Date(x.expires_on + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" })}`}` : ""}</div>` : ""}
    ${x.next_dose && x.active ? `<div class="note">Next: ${esc(whenLabel(x.next_dose))}</div>` : ""}
    <div class="row" style="margin-top:8px">
      <button class="btn small" data-logpep="${x.id}">Log dose</button>
      <button class="btn small" data-edit="${x.id}">Edit</button>
      ${x.vial_mg ? `<button class="btn small" data-vial="${x.id}">New vial</button>` : ""}
    </div>
  </div>`;
}

function historyTable(logs) {
  if (!logs.length) return `<p class="note">Nothing logged in the last 30 days.</p>`;
  return `<div class="data-table log-table" style="max-height:320px"><table>
    <thead><tr><th>When</th><th>Peptide</th><th>Dose</th><th>Units</th><th>Site</th><th></th></tr></thead>
    <tbody>${logs.map((lg) => `<tr class="${lg.status}">
      <td>${esc(new Date(lg.taken_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }))}</td>
      <td>${esc(lg.peptide_name)}${lg.status === "skipped" ? " <span class='note'>(skipped)</span>" : ""}${lg.notes ? `<br><span class="note">${esc(lg.notes)}</span>` : ""}</td>
      <td>${lg.status === "taken" ? fmtDose(lg.dose_mcg, "mcg") : "–"}</td>
      <td>${lg.units != null ? nf1.format(lg.units) : "–"}</td>
      <td>${esc(lg.site || "–")}</td>
      <td><button class="linkbtn" data-undo="${lg.id}" aria-label="Delete entry">Delete</button></td></tr>`).join("")}</tbody></table></div>`;
}

// ---------------- calculator ----------------
function calculatorHtml() {
  return `<h2 style="margin-bottom:4px">Reconstitution calculator</h2>
    <p class="note" style="margin-top:0">How much to draw on a U-100 insulin syringe (100 units = 1 mL).</p>
    <div class="calc">
      <div class="field"><label for="c-vial">Peptide in vial (mg)</label><input id="c-vial" type="number" min="0" step="any" value="5"></div>
      <div class="field"><label for="c-water">Bacteriostatic water (mL)</label><input id="c-water" type="number" min="0" step="any" value="2"></div>
      <div class="field"><label for="c-dose">Dose</label><div class="row" style="flex-wrap:nowrap"><input id="c-dose" type="number" min="0" step="any" value="250">
        <select id="c-unit" style="width:auto"><option>mcg</option><option>mg</option></select></div></div>
    </div>
    <div id="c-out" class="calc-out" aria-live="polite"></div>`;
}

function wireCalculator(card) {
  const f = () => {
    const vial = +$("#c-vial", card).value, water = +$("#c-water", card).value;
    const dose = +$("#c-dose", card).value * ($("#c-unit", card).value === "mg" ? 1000 : 1);
    const out = $("#c-out", card);
    if (!(vial > 0 && water > 0 && dose > 0)) { out.innerHTML = `<p class="note">Enter all three values.</p>`; return; }
    const conc = (vial * 1000) / water, ml = dose / conc, units = ml * 100;
    out.innerHTML = `
      <div class="calc-big">Draw <b>${nf1.format(units)}</b> units <span class="note">(${nf2.format(ml)} mL)</span></div>
      <div class="syringe" role="img" aria-label="Syringe filled to ${nf1.format(units)} of 100 units"><span style="width:${Math.min(100, units)}%"></span>
        ${[10, 20, 30, 40, 50, 60, 70, 80, 90].map((t) => `<i style="left:${t}%"></i>`).join("")}</div>
      <div class="syringe-scale"><span>0</span><span>50</span><span>100</span></div>
      <p class="note">Concentration ${nf0.format(conc)} mcg/mL · about ${nf0.format(Math.floor((vial * 1000) / dose))} doses per vial</p>
      ${units > 100 ? `<p class="err">That's more than a 1 mL syringe holds. Use more water or split the dose.</p>` : units < 2 ? `<p class="err">Under 2 units is hard to measure accurately. Consider more water.</p>` : ""}`;
  };
  card.querySelectorAll("input,select").forEach((el) => el.addEventListener("input", f));
  f();
}

// ---------------- reminders ----------------
function notifyHtml(n) {
  if (!n) {
    const topic = `home-health-${Math.random().toString(36).slice(2, 10)}`;
    return `<h2 style="margin-bottom:4px">Reminders</h2>
      <p class="note" style="margin-top:0">Get a phone notification when a peptide dose or supplement is due. Uses <b>ntfy</b>, a free push service: install the ntfy app (iPhone/Android), subscribe to the topic below, then save.</p>
      <form id="notify-form">
        <div class="field"><label for="n-topic">Topic (works like a password; keep it hard to guess)</label><input id="n-topic" value="${topic}" required pattern="[A-Za-z0-9_-]{6,64}"></div>
        <div class="field"><label for="n-server">Server</label><input id="n-server" value="https://ntfy.sh" required></div>
        <label class="check"><input type="checkbox" id="n-generic"> Hide names in notifications</label>
        <div class="row" style="margin-top:10px"><button class="btn primary">Turn on reminders</button></div>
        <p class="err" hidden></p>
      </form>`;
  }
  return `<div class="row" style="justify-content:space-between;margin-bottom:6px"><h2>Reminders</h2><span class="status linked">On</span></div>
    <p class="note" style="margin-top:0">Notifications go to topic <b>${esc(n.ntfy_topic)}</b> on ${esc(n.ntfy_server)}. In the ntfy app, subscribe to that topic to receive them. A reminder is sent when a scheduled peptide dose comes due, and at each supplement's reminder time if it isn't checked off yet. These settings are shared by the Peptides and Supplements tabs.</p>
    <label class="check"><input type="checkbox" id="n-generic" ${n.generic_text ? "checked" : ""}> Hide names in notifications</label>
    <div class="row" style="margin-top:10px">
      <button class="btn small" id="n-test">Send test</button>
      <button class="btn small danger" id="n-off">Turn off</button>
    </div>`;
}

function wireNotify(pid, n) {
  const form = $("#notify-form");
  if (form) {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const err = $(".err", form);
      try {
        await api(`/api/profiles/${pid}/notify`, { method: "PUT", body: { ntfy_topic: $("#n-topic").value.trim(), ntfy_server: $("#n-server").value.trim(), generic_text: $("#n-generic").checked } });
        toast("Reminders on"); route();
      } catch (x) { err.textContent = x.message; err.hidden = false; }
    };
    return;
  }
  $("#n-test").onclick = async () => {
    try { await api(`/api/profiles/${pid}/notify/test`, { method: "POST" }); toast("Test sent. Check your phone."); }
    catch (x) { toast(x.message); }
  };
  $("#n-off").onclick = async () => {
    if (!confirm("Turn off all reminders (peptides and supplements) for this profile?")) return;
    await api(`/api/profiles/${pid}/notify`, { method: "DELETE" }); route();
  };
  $("#n-generic").onchange = async (e) => {
    await api(`/api/profiles/${pid}/notify`, { method: "PUT", body: { ntfy_topic: n.ntfy_topic, ntfy_server: n.ntfy_server, generic_text: e.target.checked } });
    toast("Saved");
  };
}

// ---------------- log dialog ----------------
function openLogDialog(pid, { peps, sites, suggested, peptide, scheduled_for }) {
  const active = peps.filter((x) => x.active || (peptide && x.id === peptide.id));
  const first = peptide || active[0] || peps[0];
  const today = localDay();
  const when = scheduled_for && scheduled_for.slice(0, 10) !== today ? scheduled_for : localStamp();
  const d = modal(`<form method="dialog" id="log-form">
    <h2 style="margin-bottom:4px">Log dose</h2>
    ${scheduled_for ? `<p class="note" style="margin-top:0">Scheduled ${esc(whenLabel(scheduled_for))}</p>` : ""}
    <div class="field"><label for="lg-pep">Peptide</label>
      <select id="lg-pep" ${scheduled_for ? "disabled" : ""}>${(scheduled_for ? [first] : active).map((x) => `<option value="${x.id}" ${x.id === first.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></div>
    <div class="field"><label for="lg-dose">Dose</label><div class="row" style="flex-wrap:nowrap">
      <input id="lg-dose" type="number" min="0" step="any" required><span id="lg-unit" class="note"></span></div>
      <span class="note" id="lg-units"></span></div>
    <div class="field"><label for="lg-when">Time taken</label><input id="lg-when" type="datetime-local" required value="${when}"></div>
    <div class="field"><label for="lg-site">Injection site</label><select id="lg-site">
      <option value="">—</option>${sites.map((s) => `<option ${s === suggested ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
      <span class="note">Suggested: ${esc(suggested)} (least recently used)</span></div>
    <div class="field"><label for="lg-notes">Notes</label><textarea id="lg-notes" rows="2" maxlength="1000" placeholder="Optional: how you felt, side effects…"></textarea></div>
    <p class="err" hidden></p>
    <div class="row" style="justify-content:flex-end;margin-top:8px">
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="save">Log dose</button>
    </div></form>`);
  const sel = $("#lg-pep", d), dose = $("#lg-dose", d);
  const cur = () => peps.find((x) => x.id === +sel.value);
  const sync = () => {
    const x = cur(); const mg = x.dose_unit === "mg";
    dose.value = mg ? x.dose_mcg / 1000 : x.dose_mcg;
    $("#lg-unit", d).textContent = x.dose_unit;
    upd();
  };
  const upd = () => {
    const x = cur(); const mcg = +dose.value * (x.dose_unit === "mg" ? 1000 : 1);
    $("#lg-units", d).textContent = x.concentration && mcg ? `= ${fmtUnits((mcg / x.concentration) * 100)} on a U-100 syringe` : "";
    $("#lg-site", d).disabled = !["SubQ", "IM"].includes(x.route);
  };
  sel.onchange = sync; dose.oninput = upd; sync();
  $("#log-form", d).onsubmit = async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    const x = cur();
    try {
      await api(`/api/profiles/${pid}/peptide-logs`, { method: "POST", body: {
        peptide_id: x.id, status: "taken", taken_at: $("#lg-when", d).value.slice(0, 16),
        scheduled_for: scheduled_for || null, dose_mcg: +dose.value * (x.dose_unit === "mg" ? 1000 : 1),
        site: $("#lg-site", d).disabled ? null : $("#lg-site", d).value || null, notes: $("#lg-notes", d).value } });
      d.close(); toast(`${x.name} logged`); route();
    } catch (err) { const el = $(".err", d); el.textContent = err.message; el.hidden = false; }
  };
}

// ---------------- add / edit peptide ----------------
function openPeptideDialog(pid, x) {
  const editing = !!x;
  const colors = PROFILE_COLORS;
  const v = x || { name: "", color: colors[Math.floor(Math.random() * colors.length)], dose_mcg: 250, dose_unit: "mcg", route: "SubQ",
    freq: "daily", weekdays: "", interval_days: 2, times: "08:00", start_date: localDay(), end_date: null, reminders: 1, active: 1,
    vial_mg: null, water_ml: null, reconstituted_on: localDay(), expiry_days: 28, notes: "" };
  const doseVal = v.dose_unit === "mg" ? v.dose_mcg / 1000 : v.dose_mcg;
  const wd = (v.weekdays || "").split(",");
  const d = modal(`<form method="dialog" id="pep-form">
    <h2 style="margin-bottom:14px">${editing ? `Edit ${esc(v.name)}` : "Add peptide"}</h2>
    <div class="field"><label for="pp-name">Name</label><input id="pp-name" required maxlength="60" value="${esc(v.name)}" placeholder="e.g. the compound's name"></div>
    <div class="field"><label>Color</label><div class="swatches" id="pp-colors"></div></div>
    <fieldset><legend>Dose</legend>
      <div class="grid2">
        <div class="field"><label for="pp-dose">Amount per dose</label><div class="row" style="flex-wrap:nowrap">
          <input id="pp-dose" type="number" min="0" step="any" required value="${doseVal}">
          <select id="pp-unit" style="width:auto"><option ${v.dose_unit === "mcg" ? "selected" : ""}>mcg</option><option ${v.dose_unit === "mg" ? "selected" : ""}>mg</option></select></div></div>
        <div class="field"><label for="pp-route">Route</label><select id="pp-route">${ROUTES.map((r) => `<option ${r === v.route ? "selected" : ""}>${r}</option>`).join("")}</select></div>
      </div>
    </fieldset>
    <fieldset><legend>Schedule</legend>
      <div class="field"><label for="pp-freq">How often</label><select id="pp-freq">
        <option value="daily" ${v.freq === "daily" ? "selected" : ""}>Every day</option>
        <option value="weekdays" ${v.freq === "weekdays" ? "selected" : ""}>Specific days of the week</option>
        <option value="interval" ${v.freq === "interval" ? "selected" : ""}>Every few days</option>
        <option value="as_needed" ${v.freq === "as_needed" ? "selected" : ""}>As needed (no schedule)</option></select></div>
      <div class="field" id="pp-wd-box"><label>Days</label><div class="daypicks">${WEEKDAYS.map((n, i) => `<label><input type="checkbox" value="${i}" ${wd.includes(String(i)) ? "checked" : ""}><span>${n}</span></label>`).join("")}</div></div>
      <div class="field" id="pp-int-box"><label for="pp-int">Every how many days</label><input id="pp-int" type="number" min="1" max="90" value="${v.interval_days}"></div>
      <div class="field" id="pp-times-box"><label>Dose times</label><div id="pp-times"></div>
        <button type="button" class="linkbtn" id="pp-addtime">＋ Add a time</button></div>
      <div class="grid2">
        <div class="field"><label for="pp-start">Start date</label><input id="pp-start" type="date" required value="${esc(v.start_date)}"></div>
        <div class="field"><label for="pp-end">End date (optional)</label><input id="pp-end" type="date" value="${esc(v.end_date || "")}"></div>
      </div>
      <label class="check"><input type="checkbox" id="pp-remind" ${v.reminders ? "checked" : ""}> Send reminders</label>
    </fieldset>
    <fieldset><legend>Vial (optional)</legend>
      <div class="grid2">
        <div class="field"><label for="pp-vial">Peptide in vial (mg)</label><input id="pp-vial" type="number" min="0" step="any" value="${v.vial_mg ?? ""}"></div>
        <div class="field"><label for="pp-water">Water added (mL)</label><input id="pp-water" type="number" min="0" step="any" value="${v.water_ml ?? ""}"></div>
        <div class="field"><label for="pp-recon">Mixed on</label><input id="pp-recon" type="date" value="${esc(v.reconstituted_on || "")}"></div>
        <div class="field"><label for="pp-exp">Discard after (days)</label><input id="pp-exp" type="number" min="1" max="365" value="${v.expiry_days}"></div>
      </div>
      <p class="note" id="pp-calc"></p>
    </fieldset>
    <div class="field"><label for="pp-notes">Notes</label><textarea id="pp-notes" rows="2" maxlength="1000">${esc(v.notes || "")}</textarea></div>
    ${editing ? `<label class="check"><input type="checkbox" id="pp-active" ${v.active ? "checked" : ""}> Active (uncheck to pause without deleting history)</label>` : ""}
    <p class="err" hidden></p>
    <div class="row" style="margin-top:12px">
      ${editing ? `<button type="button" class="btn danger" id="pp-del">Delete</button>` : ""}
      <span class="spacer"></span>
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="save">Save</button>
    </div></form>`, "wide");

  renderSwatches($("#pp-colors", d), v.color);
  const timesEl = $("#pp-times", d);
  const addTime = (t) => {
    const row = document.createElement("div");
    row.className = "row time-row";
    row.innerHTML = `<input type="time" value="${esc(t)}" required><button type="button" class="linkbtn" aria-label="Remove time">Remove</button>`;
    $("button", row).onclick = () => { if (timesEl.children.length > 1) row.remove(); };
    timesEl.appendChild(row);
  };
  (v.times || "08:00").split(",").filter(Boolean).forEach(addTime);
  $("#pp-addtime", d).onclick = () => { if (timesEl.children.length < 6) addTime("20:00"); };

  const freq = $("#pp-freq", d);
  const showFreq = () => {
    $("#pp-wd-box", d).hidden = freq.value !== "weekdays";
    $("#pp-int-box", d).hidden = freq.value !== "interval";
    $("#pp-times-box", d).hidden = freq.value === "as_needed";
    timesEl.querySelectorAll("input").forEach((i) => { i.required = freq.value !== "as_needed"; });
  };
  freq.onchange = showFreq; showFreq();

  const calc = () => {
    const vial = +$("#pp-vial", d).value, water = +$("#pp-water", d).value;
    const mcg = +$("#pp-dose", d).value * ($("#pp-unit", d).value === "mg" ? 1000 : 1);
    $("#pp-calc", d).textContent = vial > 0 && water > 0 && mcg > 0
      ? `${nf0.format((vial * 1000) / water)} mcg/mL, so each dose is ${nf1.format((mcg / ((vial * 1000) / water)) * 100)} units on a U-100 syringe (about ${Math.floor((vial * 1000) / mcg)} doses per vial).`
      : "Add the vial size and water to see how many units to draw.";
  };
  d.querySelectorAll("#pp-vial,#pp-water,#pp-dose,#pp-unit").forEach((el) => el.addEventListener("input", calc));
  calc();

  if (editing) $("#pp-del", d).onclick = async () => {
    if (!confirm(`Delete ${v.name}? Its schedule is removed; past log entries stay in the history.`)) return;
    await api(`/api/profiles/${pid}/peptides/${v.id}`, { method: "DELETE" });
    d.close(); toast("Deleted"); route();
  };

  $("#pep-form", d).onsubmit = async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    const num = (id) => { const s = $(id, d).value; return s === "" ? null : +s; };
    const body = {
      name: $("#pp-name", d).value, color: $("#pp-colors", d).dataset.value,
      dose_mcg: (+$("#pp-dose", d).value || 0) * ($("#pp-unit", d).value === "mg" ? 1000 : 1), dose_unit: $("#pp-unit", d).value,
      route: $("#pp-route", d).value, freq: freq.value,
      weekdays: [...d.querySelectorAll(".daypicks input:checked")].map((i) => i.value).join(","),
      interval_days: +$("#pp-int", d).value || 1,
      times: [...timesEl.querySelectorAll("input")].map((i) => i.value).filter(Boolean).join(","),
      start_date: $("#pp-start", d).value, end_date: $("#pp-end", d).value || null,
      reminders: $("#pp-remind", d).checked, active: editing ? $("#pp-active", d).checked : true,
      vial_mg: num("#pp-vial"), water_ml: num("#pp-water"), reconstituted_on: $("#pp-recon", d).value || null,
      expiry_days: +$("#pp-exp", d).value || 28, notes: $("#pp-notes", d).value,
    };
    try {
      await api(`/api/profiles/${pid}/peptides${editing ? `/${v.id}` : ""}`, { method: editing ? "PUT" : "POST", body });
      d.close(); toast(editing ? "Saved" : `${body.name} added`); route();
    } catch (err) { const el = $(".err", d); el.textContent = err.message.replace(/Value error, /g, ""); el.hidden = false; }
  };
}
