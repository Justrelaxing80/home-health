/* Home Health - supplements: daily checklist, 14-day consistency grid, streaks, popular list.
   Uses helpers from app.js and peptides.js ($, esc, api, toast, state, navTabs, setTopbar,
   modal, localDay, addDays, WEEKDAYS, dayLabel, ...). */
"use strict";

const fmtAmount = (s) => (s.amount != null ? `${nf2.format(s.amount)}${s.unit ? " " + esc(s.unit) : ""}` : esc(s.unit || ""));
const days = (n) => `${n} day${n === 1 ? "" : "s"}`;

function suppSchedule(s) {
  const when = s.freq === "weekdays" ? s.weekdays.split(",").filter(Boolean).map((w) => WEEKDAYS[+w]).join(", ") : "Daily";
  return `${when}${s.servings > 1 ? ` · ${s.servings}× a day` : ""}${s.time_label ? ` · ${esc(s.time_label)}` : ""}`;
}

async function renderSupplements(pid) {
  const p = state.profiles.find((x) => x.id === pid);
  if (!p) { location.hash = "#/"; return; }
  store.set("lastProfile", String(pid));
  setTopbar(p, { section: "supplements" });
  if (!state.supDay || state.supPid !== pid) { state.supDay = null; state.supPid = pid; }

  let data;
  try { data = await api(`/api/profiles/${pid}/supplements${state.supDay ? `?day=${state.supDay}` : ""}`); }
  catch (e) { $("#view").innerHTML = `${navTabs(pid, "supplements")}<p class="err">${esc(e.message)}</p>`; return; }
  const today = data.today, day = data.day;
  const sups = data.supplements, byId = Object.fromEntries(sups.map((s) => [s.id, s]));
  const due = sups.filter((s) => s.scheduled);
  const active = sups.filter((s) => s.active);
  const st = data.streak;
  const added = new Set(sups.map((s) => s.name.toLowerCase()));

  const tiles = [
    { label: day === today ? "Today" : dayLabel(day), value: data.servings_due ? `${data.servings_taken}/${data.servings_due}` : "–",
      unit: data.servings_due ? "servings" : "", meta: data.servings_due ? (data.servings_taken >= data.servings_due ? "All done" : `${data.servings_due - data.servings_taken} left`) : "Nothing scheduled" },
    { label: "Current streak", value: st.current, unit: st.current === 1 ? "day" : "days",
      meta: st.today === "done" ? "Today counted" : st.today === "missed" ? "Finish today to extend it" : "Every scheduled supplement, every day" },
    { label: "Best streak", value: st.best, unit: st.best === 1 ? "day" : "days", meta: "Last 2 years" },
    { label: "Supplements", value: active.length, unit: "active", meta: sups.length > active.length ? `${sups.length - active.length} paused` : "" },
  ];

  $("#view").innerHTML = `
    ${navTabs(pid, "supplements")}
    <div class="dash-head">
      <div><h1>Supplements</h1><p class="sub" style="margin:0">Daily checklist and streaks for ${esc(p.name)}</p></div>
      <button class="btn primary" id="sup-add">＋ Add supplement</button>
    </div>
    <div class="tiles">${tiles.map((x) => `
      <div class="tile"><div class="label">${esc(x.label)}</div>
        <div class="value">${esc(x.value)}${x.unit ? `<small>${esc(x.unit)}</small>` : ""}</div>
        <div class="meta">${esc(x.meta)}</div></div>`).join("")}</div>
    <div class="pep-grid">
      <section class="card">
        <div class="day-nav">
          <button class="btn small" data-sday="-1" aria-label="Previous day">‹</button>
          <h2>${esc(dayLabel(day))} <span class="note">${esc(new Date(day + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" }))}</span></h2>
          ${day < today ? `<button class="btn small" data-sday="1" aria-label="Next day">›</button><button class="btn small" data-sday="0">Today</button>` : ""}
        </div>
        ${due.length ? `<ul class="doses">${due.map((s) => checkRow(s)).join("")}</ul>`
          : `<p class="note" style="padding:18px 0;text-align:center">${sups.length ? "Nothing scheduled for this day." : "No supplements yet. Pick some from the popular list or add your own."}</p>`}
      </section>
      ${active.length ? `<section class="card">
        <h2 style="margin-bottom:4px">Last 14 days</h2>
        <p class="note" style="margin-top:0">Each square is a day. Streaks count days where every serving was checked off.</p>
        ${gridHtml(data, active)}
      </section>` : ""}
      ${sups.length ? `<section class="card">
        <h2 style="margin-bottom:10px">My supplements</h2>
        ${sups.map((s) => `<div class="vial${s.active ? "" : " paused"}">
          <div class="row" style="justify-content:space-between">
            <b class="row" style="gap:8px"><span class="pdot" style="background:${esc(s.color)}"></span>${esc(s.name)}</b>
            <span class="row" style="gap:8px">${s.active ? `<span class="chip ${s.streak.current ? "taken" : ""}">${s.streak.current}-day streak</span>` : `<span class="chip">Paused</span>`}
              <button class="btn small" data-sedit="${s.id}">Edit</button></span>
          </div>
          <div class="note">${fmtAmount(s)} · ${suppSchedule(s)}${s.active && s.streak.best > s.streak.current ? ` · best ${days(s.streak.best)}` : ""}</div>
          ${s.notes ? `<div class="note">${esc(s.notes)}</div>` : ""}
        </div>`).join("")}
      </section>` : ""}
      <section class="card">
        <h2 style="margin-bottom:4px">Popular supplements</h2>
        <p class="note" style="margin-top:0">Tap to add with a typical label serving, then edit it to match your product.</p>
        <div class="catalog">${data.catalog.map((c, i) => {
          const has = added.has(c.name.toLowerCase());
          return `<button class="cat-item" data-cat="${i}" ${has ? "disabled" : ""}><b>${has ? "✓ " : "＋ "}${esc(c.name)}</b><span>${nf2.format(c.amount)} ${esc(c.unit)}</span></button>`;
        }).join("")}<button class="cat-item custom" id="sup-custom"><b>＋ Something else</b><span>Add your own</span></button></div>
      </section>
    </div>
    <p class="note disclaimer">For personal tracking only. Amounts shown are common label servings, not recommendations.</p>`;

  // ---- wiring ----
  document.querySelectorAll("[data-sday]").forEach((b) => b.onclick = () => {
    const n = +b.dataset.sday;
    const next = n === 0 ? today : addDays(day, n);
    state.supDay = next >= today ? null : next;
    route();
  });
  const add = () => openSupplementDialog(pid, null, data);
  $("#sup-add").onclick = add;
  $("#sup-custom").onclick = add;
  document.querySelectorAll("[data-sedit]").forEach((b) => b.onclick = () => openSupplementDialog(pid, byId[b.dataset.sedit], data));
  document.querySelectorAll("[data-cat]").forEach((b) => b.onclick = async () => {
    const c = data.catalog[+b.dataset.cat];
    b.disabled = true;
    try {
      await api(`/api/profiles/${pid}/supplements`, { method: "POST", body: {
        name: c.name, amount: c.amount, unit: c.unit, time_label: c.time_label,
        color: PROFILE_COLORS[sups.length % PROFILE_COLORS.length] } });
      toast(`${c.name} added`); route();
    } catch (e) { b.disabled = false; toast(e.message); }
  });
  document.querySelectorAll("[data-serv]").forEach((b) => b.onclick = async () => {
    const [sid, n] = b.dataset.serv.split("|");
    const s = byId[sid], taken = b.getAttribute("aria-pressed") !== "true";
    b.disabled = true;
    try {
      await api(`/api/profiles/${pid}/supplements/${sid}/serving`, { method: "PUT", body: { date: day, serving: +n, taken } });
      route();
    } catch (e) { b.disabled = false; toast(e.message); }
    if (taken && s) b.blur();
  });
}

function checkRow(s) {
  const done = s.taken >= s.servings;
  const buttons = Array.from({ length: s.servings }, (_, i) => {
    const on = i < s.taken;
    const label = s.servings === 1 ? (on ? "✓ Taken" : "Take") : (on ? `✓ ${i + 1}` : `${i + 1}`);
    return `<button class="serv${on ? " on" : ""}" aria-pressed="${on}" data-serv="${s.id}|${i + 1}"
      aria-label="${esc(s.name)} serving ${i + 1} of ${s.servings}${on ? ", taken" : ""}">${label}</button>`;
  }).join("");
  return `<li class="dose sup ${done ? "taken" : ""}">
    <span class="pdot" style="background:${esc(s.color)}"></span>
    <div class="dose-main"><b>${esc(s.name)}</b><span class="note">${fmtAmount(s)}${s.time_label ? ` · ${esc(s.time_label)}` : ""}${s.servings > 1 ? ` · ${s.taken}/${s.servings}` : ""}</span></div>
    <div class="serv-row">${buttons}</div>
  </li>`;
}

function gridHtml(data, active) {
  const t = data.today;
  const head = data.grid_days.map((g) => {
    const d = new Date(g + "T12:00:00");
    return `<span class="g-head${g === t ? " now" : ""}" title="${esc(d.toLocaleDateString())}">${d.toLocaleDateString(undefined, { weekday: "narrow" })}</span>`;
  }).join("");
  const label = { done: "all taken", partial: "some taken", missed: "missed", today: "not yet" };
  const rows = active.map((s) => `<div class="g-name" title="${esc(s.name)}"><span class="pdot" style="background:${esc(s.color)}"></span>${esc(s.name)}</div>
    ${s.grid.map((v, i) => `<span class="g-cell ${v || "off"}" title="${esc(new Date(data.grid_days[i] + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }))}: ${v ? label[v] : "not scheduled"}"></span>`).join("")}`).join("");
  return `<div class="sgrid"><span></span>${head}${rows}</div>
    <div class="legend" style="margin-top:10px">
      <span><i class="g-cell done"></i>All taken</span><span><i class="g-cell partial"></i>Some</span>
      <span><i class="g-cell missed"></i>Missed</span><span><i class="g-cell off"></i>Not scheduled</span></div>`;
}

function openSupplementDialog(pid, x, data) {
  const editing = !!x;
  const v = x || { name: "", color: PROFILE_COLORS[data.supplements.length % PROFILE_COLORS.length], amount: null, unit: "mg",
    servings: 1, freq: "daily", weekdays: "", time_label: "Morning", notes: "", active: 1 };
  const wd = (v.weekdays || "").split(",");
  const units = data.units.includes(v.unit) || !v.unit ? data.units : [v.unit, ...data.units];
  const d = modal(`<form method="dialog" id="sup-form">
    <h2 style="margin-bottom:14px">${editing ? `Edit ${esc(v.name)}` : "Add supplement"}</h2>
    <div class="field"><label for="sp-name">Name</label><input id="sp-name" required maxlength="60" value="${esc(v.name)}" placeholder="e.g. Vitamin B12" list="sp-names"></div>
    <datalist id="sp-names">${data.catalog.map((c) => `<option value="${esc(c.name)}">`).join("")}</datalist>
    <div class="field"><label>Color</label><div class="swatches" id="sp-colors"></div></div>
    <div class="grid2">
      <div class="field"><label for="sp-amount">Amount per serving</label><input id="sp-amount" type="number" min="0" step="any" value="${v.amount ?? ""}" placeholder="optional"></div>
      <div class="field"><label for="sp-unit">Unit</label><select id="sp-unit">${units.map((u) => `<option ${u === v.unit ? "selected" : ""}>${esc(u)}</option>`).join("")}</select></div>
      <div class="field"><label for="sp-serv">Servings per day</label><select id="sp-serv">${[1, 2, 3, 4, 5, 6].map((n) => `<option ${n === v.servings ? "selected" : ""}>${n}</option>`).join("")}</select></div>
      <div class="field"><label for="sp-time">When</label><select id="sp-time">${data.time_labels.map((t) => `<option value="${esc(t)}" ${t === v.time_label ? "selected" : ""}>${t ? esc(t) : "Any time"}</option>`).join("")}</select></div>
    </div>
    <div class="field"><label for="sp-freq">How often</label><select id="sp-freq">
      <option value="daily" ${v.freq === "daily" ? "selected" : ""}>Every day</option>
      <option value="weekdays" ${v.freq === "weekdays" ? "selected" : ""}>Specific days of the week</option></select></div>
    <div class="field" id="sp-wd-box"><label>Days</label><div class="daypicks">${WEEKDAYS.map((n, i) => `<label><input type="checkbox" value="${i}" ${wd.includes(String(i)) ? "checked" : ""}><span>${n}</span></label>`).join("")}</div></div>
    <div class="field"><label for="sp-notes">Notes</label><textarea id="sp-notes" rows="2" maxlength="500" placeholder="Brand, with food, etc.">${esc(v.notes || "")}</textarea></div>
    ${editing ? `<label class="check"><input type="checkbox" id="sp-active" ${v.active ? "checked" : ""}> Active (uncheck to pause; paused supplements don't affect streaks)</label>` : ""}
    <p class="err" hidden></p>
    <div class="row" style="margin-top:12px">
      ${editing ? `<button type="button" class="btn danger" id="sp-del">Delete</button>` : ""}
      <span class="spacer"></span>
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="save">Save</button>
    </div></form>`, "wide");

  renderSwatches($("#sp-colors", d), v.color);
  const freq = $("#sp-freq", d);
  const showFreq = () => { $("#sp-wd-box", d).hidden = freq.value !== "weekdays"; };
  freq.onchange = showFreq; showFreq();
  // Picking a popular name fills in its typical serving (new supplements only).
  if (!editing) $("#sp-name", d).addEventListener("change", (e) => {
    const c = data.catalog.find((c) => c.name.toLowerCase() === e.target.value.trim().toLowerCase());
    if (!c) return;
    $("#sp-amount", d).value = c.amount; $("#sp-unit", d).value = c.unit; $("#sp-time", d).value = c.time_label;
  });

  if (editing) $("#sp-del", d).onclick = async () => {
    if (!confirm(`Delete ${v.name} and its check-off history?`)) return;
    await api(`/api/profiles/${pid}/supplements/${v.id}`, { method: "DELETE" });
    d.close(); toast("Deleted"); route();
  };

  $("#sup-form", d).onsubmit = async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    const amt = $("#sp-amount", d).value;
    const body = {
      name: $("#sp-name", d).value, color: $("#sp-colors", d).dataset.value,
      amount: amt === "" ? null : +amt, unit: $("#sp-unit", d).value, servings: +$("#sp-serv", d).value,
      freq: freq.value, weekdays: [...d.querySelectorAll(".daypicks input:checked")].map((i) => i.value).join(","),
      time_label: $("#sp-time", d).value, notes: $("#sp-notes", d).value,
      active: editing ? $("#sp-active", d).checked : true,
    };
    if (editing && body.servings < v.servings && !confirm(`Lower to ${body.servings} serving(s) a day? Check-offs above that are removed.`)) return;
    try {
      await api(`/api/profiles/${pid}/supplements${editing ? `/${v.id}` : ""}`, { method: editing ? "PUT" : "POST", body });
      d.close(); toast(editing ? "Saved" : `${body.name} added`); route();
    } catch (err) { const el = $(".err", d); el.textContent = err.message.replace(/Value error, /g, ""); el.hidden = false; }
  };
}
