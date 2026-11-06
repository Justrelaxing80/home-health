/* Home Health - barcode scanner for supplements.
   Full-screen camera with a scan window; the result opens as a bottom sheet with
   the product's label info. Live scanning needs a secure context (HTTPS or
   localhost); otherwise it falls back to taking a photo of the barcode, which
   works over plain http too. Decoding: the browser's BarcodeDetector when it
   exists (Chrome/Android), else ZXing (vendored, loaded on first use). */
"use strict";

const SCAN_FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e"];
let zxingLoad = null;

function loadZxing() {
  if (window.ZXing) return Promise.resolve();
  if (!zxingLoad) zxingLoad = new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = "/static/vendor/zxing.min.js";
    s.onload = ok; s.onerror = () => { zxingLoad = null; fail(new Error("Couldn't load the barcode reader")); };
    document.head.appendChild(s);
  });
  return zxingLoad;
}

async function nativeDetector() {
  if (!("BarcodeDetector" in window)) return null;
  try {
    const have = await BarcodeDetector.getSupportedFormats();
    const fmts = SCAN_FORMATS.filter((f) => have.includes(f));
    return fmts.length ? new BarcodeDetector({ formats: fmts }) : null;
  } catch { return null; }
}

function zxingReader() {
  const Z = window.ZXing;
  const hints = new Map();
  hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8, Z.BarcodeFormat.UPC_A, Z.BarcodeFormat.UPC_E]);
  hints.set(Z.DecodeHintType.TRY_HARDER, true);
  const r = new Z.MultiFormatReader();
  r.setHints(hints);
  return r;
}

function zxingDecode(reader, canvas) {
  const Z = window.ZXing;
  try {
    const bmp = new Z.BinaryBitmap(new Z.HybridBinarizer(new Z.HTMLCanvasElementLuminanceSource(canvas)));
    return reader.decodeWithState(bmp).getText();
  } catch { return null; } finally { reader.reset(); }
}

// Decode a still image (photo mode). Tries the native detector, then ZXing, then ZXing rotated
// 90° (bottles often carry the barcode running up the side).
async function decodeImage(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((ok, fail) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => fail(new Error("Couldn't open that photo")); i.src = url; });
    const det = await nativeDetector();
    if (det) {
      const r = await det.detect(img).catch(() => []);
      if (r.length) return r[0].rawValue;
    }
    await loadZxing();
    const reader = zxingReader();
    for (const max of [1600, 1000]) {
      const k = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
      const w = Math.round(img.naturalWidth * k), h = Math.round(img.naturalHeight * k);
      for (const rot of [0, 90]) {
        const c = document.createElement("canvas");
        c.width = rot ? h : w; c.height = rot ? w : h;
        const g = c.getContext("2d", { willReadFrequently: true });
        if (rot) { g.translate(c.width, 0); g.rotate(Math.PI / 2); }
        g.drawImage(img, 0, 0, w, h);
        const t = zxingDecode(reader, c);
        if (t) return t;
      }
    }
    return null;
  } finally { URL.revokeObjectURL(url); }
}

const ICON = {
  close: '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
  torch: '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M8 2h8v4l-2 4v11a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V10L8 6z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 13v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  keys: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><rect x="2.5" y="6" width="19" height="12" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M6 10h1M9.5 10h1M13 10h1M16.5 10h1M7 14h10" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  photo: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M4 8h3l2-2.5h6L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><circle cx="12" cy="13.2" r="3.4" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  barcode: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M3 5v14M6 5v14M9.5 5v14M12 5v14M15.5 5v14M18 5v14M21 5v14" stroke="currentColor" stroke-width="1.6" stroke-dasharray="14 0"/></svg>',
};

/* Entry point. `data` is the supplements page payload (catalog, units, time labels, list). */
async function openScanner(pid, data) {
  const live = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
  const el = document.createElement("div");
  el.className = "scanner";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-modal", "true");
  el.setAttribute("aria-label", "Scan a supplement barcode");
  el.innerHTML = `
    <video playsinline muted autoplay></video>
    <div class="scan-shade" aria-hidden="true"><div class="scan-window"><i></i><i></i><i></i><i></i><b class="scan-line"></b></div></div>
    <div class="scan-top">
      <button class="scan-icon" data-x="close" aria-label="Close scanner">${ICON.close}</button>
      <span class="scan-title">Scan barcode</span>
      <button class="scan-icon" data-x="torch" aria-label="Flashlight" style="visibility:hidden" tabindex="-1">${ICON.torch}</button>
    </div>
    <div class="scan-photo-mode" hidden>
      <div class="scan-illus">${ICON.barcode.replace(/18/g, "64")}</div>
      <h2>Take a photo of the barcode</h2>
      <p>Live scanning needs a secure (https) connection, so this uses your camera's photo mode. Fill the frame with the barcode and keep it sharp.</p>
    </div>
    <div class="scan-bottom">
      <p class="scan-hint">Line up the barcode inside the frame</p>
      <div class="scan-actions">
        <button class="scan-pill" data-x="manual">${ICON.keys}<span>Enter code</span></button>
        <button class="scan-pill" data-x="photo">${ICON.photo}<span>Use a photo</span></button>
      </div>
    </div>
    <input type="file" accept="image/*" capture="environment" hidden>
    <div class="sheet" hidden><div class="sheet-grip" aria-hidden="true"></div><div class="sheet-body"></div></div>`;
  document.body.appendChild(el);
  document.documentElement.classList.add("no-scroll");

  const video = $("video", el), sheet = $(".sheet", el), body = $(".sheet-body", el), file = $("input[type=file]", el);
  let stream = null, timer = null, busy = false, changed = false, closed = false;

  const stopCamera = () => { clearTimeout(timer); timer = null; if (stream) stream.getTracks().forEach((t) => t.stop()); stream = null; };
  const close = () => {
    if (closed) return;
    closed = true; stopCamera(); el.remove(); document.documentElement.classList.remove("no-scroll");
    document.removeEventListener("keydown", onKey);
    if (changed) route();
  };
  const onKey = (e) => { if (e.key === "Escape") { if (!sheet.hidden && stream) resume(); else close(); } };
  document.addEventListener("keydown", onKey);

  const hideSheet = () => { sheet.hidden = true; el.classList.remove("has-sheet"); };
  const showSheet = (html) => { body.innerHTML = html; sheet.hidden = false; el.classList.add("has-sheet"); };
  const resume = () => { hideSheet(); busy = false; if (stream) { video.play().catch(() => {}); loop(); } };

  // ---- camera ----
  async function startCamera() {
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } } });
    } catch (e) {
      photoMode(e.name === "NotAllowedError" ? "Camera access was blocked. Allow it in the browser settings, or take a photo instead." : null);
      return;
    }
    if (closed) { stopCamera(); return; }
    video.srcObject = stream;
    await video.play().catch(() => {});
    const track = stream.getVideoTracks()[0];
    const caps = track.getCapabilities ? track.getCapabilities() : {};
    if (caps.torch) {
      const b = $('[data-x="torch"]', el); b.style.visibility = ""; b.tabIndex = 0; let on = false;
      b.onclick = async () => { on = !on; try { await track.applyConstraints({ advanced: [{ torch: on }] }); b.classList.toggle("on", on); } catch { on = !on; } };
    }
    if (caps.focusMode && caps.focusMode.includes("continuous")) track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(() => {});
    detector = await nativeDetector();
    if (!detector) { try { await loadZxing(); reader = zxingReader(); } catch (e) { toast(e.message); } }
    loop();
  }

  let detector = null, reader = null;
  const crop = document.createElement("canvas");
  const cg = crop.getContext("2d", { willReadFrequently: true });
  let last = "", lastCount = 0;

  function loop() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (closed || busy || !stream || video.readyState < 2) { if (!closed && !busy) loop(); return; }
      let code = null;
      try {
        if (detector) {
          const r = await detector.detect(video);
          if (r.length) code = r[0].rawValue;
        } else if (reader) {
          // Center band of the frame (where the scan window is), scaled to <= 960px wide.
          const vw = video.videoWidth, vh = video.videoHeight;
          const sw = vw * 0.9, sh = Math.min(vh * 0.5, sw * 0.6);
          const k = Math.min(1, 960 / sw);
          crop.width = Math.round(sw * k); crop.height = Math.round(sh * k);
          cg.drawImage(video, (vw - sw) / 2, (vh - sh) / 2, sw, sh, 0, 0, crop.width, crop.height);
          code = zxingDecode(reader, crop);
        }
      } catch { /* keep scanning */ }
      // ZXing occasionally misreads a single frame; require the same code twice.
      if (code && (detector || code === last)) { found(code); return; }
      if (code) { last = code; lastCount = 1; } else if (++lastCount > 8) { last = ""; }
      loop();
    }, 110);
  }

  function photoMode(msg) {
    el.classList.add("photo");
    $(".scan-photo-mode", el).hidden = false;
    if (msg) $(".scan-photo-mode p", el).textContent = msg;
    $(".scan-hint", el).hidden = true;
    const b = $('[data-x="photo"]', el);
    b.classList.add("primary"); $("span", b).textContent = "Take photo";
  }

  // ---- results ----
  async function found(raw) {
    busy = true; clearTimeout(timer);
    if (navigator.vibrate) navigator.vibrate(35);
    if (stream) video.pause();
    await lookup(raw);
  }

  async function lookup(raw) {
    const code = String(raw).replace(/\D/g, "");
    showSheet(`<div class="sheet-loading"><span class="spinner" aria-hidden="true"></span>Looking up <b>${esc(code)}</b>…</div>`);
    let r;
    try { r = await api(`/api/profiles/${pid}/barcode/${encodeURIComponent(code)}`); }
    catch (e) { return showError(e.message, code); }
    if (closed) return;
    if (r.existing_id) return showExisting(r);
    if (!r.found) return r.error && !r.code ? showError(r.error, code) : showNotFound(r);
    showProduct(r);
  }

  const againBtn = () => `<button class="btn" data-x="again">${stream ? "Scan another" : "Try another photo"}</button>`;
  const wireAgain = () => { const b = $('[data-x="again"]', body); if (b) b.onclick = () => (stream ? resume() : (hideSheet(), file.click())); };

  function showError(msg, code) {
    showSheet(`<h2 class="sheet-title">Couldn't look that up</h2>
      <p class="note">${esc(msg)}</p>
      <div class="sheet-actions">${againBtn()}${code ? `<button class="btn primary" data-x="retry">Try again</button>` : ""}</div>`);
    wireAgain();
    const rb = $('[data-x="retry"]', body); if (rb) rb.onclick = () => lookup(code);
  }

  function showNotFound(r) {
    showSheet(`<h2 class="sheet-title">No match for this barcode</h2>
      <p class="note">${esc(fmtCode(r.code))} isn't in Open Food Facts or UPCitemdb yet${r.error ? ` (${esc(r.error)})` : ""}. Add it yourself and this barcode will be recognized next time.</p>
      <div class="sheet-actions">${againBtn()}<button class="btn primary" data-x="manual-add">Add it manually</button></div>`);
    wireAgain();
    $('[data-x="manual-add"]', body).onclick = () => {
      close();
      openSupplementDialog(pid, null, data, { barcode: r.code });
    };
  }

  function productHead(r) {
    return `<div class="prod-head">
      ${r.image_url ? `<img class="prod-img" src="${esc(r.image_url)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">` : `<div class="prod-img ph">${ICON.barcode}</div>`}
      <div class="prod-id"><h2 class="sheet-title">${esc(r.name)}</h2>
        ${r.brand ? `<div class="prod-brand">${esc(r.brand)}</div>` : ""}
        <div class="prod-code">${esc(fmtCode(r.code))}</div></div></div>`;
  }

  function showExisting(r) {
    const s = data.supplements.find((x) => x.id === r.existing_id);
    showSheet(`${productHead({ ...r, name: s ? s.name : r.name, image_url: (s && s.image_url) || r.image_url })}
      <p class="sheet-banner">✓ Already in your supplements</p>
      <div class="sheet-status note">Checking today…</div>
      <div class="sheet-actions">${againBtn()}<button class="btn primary" data-x="take" disabled>Mark taken</button></div>`);
    wireAgain();
    refreshTake(r.existing_id);
  }

  async function refreshTake(sid) {
    let today;
    try { today = await api(`/api/profiles/${pid}/supplements`); } catch (e) { $(".sheet-status", body).textContent = e.message; return; }
    const s = today.supplements.find((x) => x.id === sid);
    const btn = $('[data-x="take"]', body), status = $(".sheet-status", body);
    if (!s) { status.textContent = "This supplement was removed."; return; }
    if (!s.active) { status.textContent = "It's paused. Turn it back on with Edit to track it."; btn.hidden = true; return; }
    if (!s.scheduled) { status.textContent = "Not scheduled today."; btn.hidden = true; return; }
    const left = s.servings - s.taken;
    status.textContent = left <= 0 ? `All ${s.servings > 1 ? `${s.servings} servings` : "taken"} for today.` : `Today: ${s.taken} of ${s.servings} serving${s.servings > 1 ? "s" : ""} taken.`;
    if (left <= 0) { btn.hidden = true; return; }
    btn.disabled = false;
    btn.textContent = s.servings > 1 ? `Mark serving ${s.taken + 1} taken` : "Mark taken today";
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api(`/api/profiles/${pid}/supplements/${sid}/serving`, { method: "PUT", body: { date: today.today, serving: s.taken + 1, taken: true } });
        changed = true;
        if (navigator.vibrate) navigator.vibrate([20, 40, 20]);
        toast(`${s.name} checked off`);
        refreshTake(sid);
      } catch (e) { btn.disabled = false; toast(e.message); }
    };
  }

  function showProduct(r) {
    const nut = r.nutrition || [];
    const big = nut.filter((n) => ["Calories", "Protein", "Carbs", "Fat"].includes(n.label));
    const rest = nut.filter((n) => !big.includes(n));
    const units = data.units.includes(r.unit) || !r.unit ? data.units : [r.unit, ...data.units];
    const unit = r.unit || "serving";
    showSheet(`${productHead(r)}
      <dl class="prod-facts">
        ${r.serving_text ? `<div><dt>Serving</dt><dd>${esc(r.serving_text)}</dd></div>` : ""}
        ${r.package ? `<div><dt>Package</dt><dd>${esc(r.package)}</dd></div>` : ""}
      </dl>
      ${big.length ? `<div class="macro-row">${big.map((n) => `<div class="macro"><b>${esc(nf2.format(n.value))}<small>${n.unit === "kcal" ? "" : esc(n.unit)}</small></b><span>${esc(n.label)}</span></div>`).join("")}</div>` : ""}
      ${rest.length ? `<p class="note prod-micro">${rest.map((n) => `${esc(n.label)} ${esc(nf2.format(n.value))} ${esc(n.unit)}`).join(" · ")}</p>` : ""}
      ${nut.length ? `<p class="note prod-per">Per serving, as listed by ${esc(r.source.split(" + ")[0])}.</p>` : ""}
      <form class="sheet-form" autocomplete="off">
        <div class="grid2">
          <div class="field"><label for="sc-amount">Amount per serving</label><input id="sc-amount" type="number" min="0" step="any" value="${r.amount ?? ""}" placeholder="from the label"></div>
          <div class="field"><label for="sc-unit">Unit</label><select id="sc-unit">${units.map((u) => `<option ${u === unit ? "selected" : ""}>${esc(u)}</option>`).join("")}</select></div>
        </div>
        <div class="field"><label>Servings per day</label><div class="serv-seg" id="sc-serv">${[1, 2, 3, 4].map((n) => `<button type="button" data-n="${n}" aria-pressed="${n === 1}">${n}</button>`).join("")}</div></div>
        <div class="field"><label for="sc-time">When</label><select id="sc-time">${data.time_labels.map((t) => `<option value="${esc(t)}" ${t === "Morning" ? "selected" : ""}>${t ? esc(t) : "Any time"}</option>`).join("")}</select></div>
        <p class="note prod-src">Product info from <a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.source)}</a>. Check it against your label.</p>
        <p class="err" hidden></p>
        <div class="sheet-actions">
          <button type="button" class="btn" data-x="more">More options</button>
          <button class="btn primary" data-x="add">Add to supplements</button>
        </div>
        <div class="sheet-actions" style="margin-top:6px">${againBtn()}</div>
      </form>`);
    wireAgain();
    let servings = 1;
    body.querySelectorAll("#sc-serv button").forEach((b) => b.onclick = () => {
      servings = +b.dataset.n;
      body.querySelectorAll("#sc-serv button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    });
    const values = () => {
      const amt = $("#sc-amount", body).value;
      return { name: r.name.slice(0, 60), brand: r.brand || "", image_url: r.image_url || "", barcode: r.code,
        amount: amt === "" ? null : +amt, unit: $("#sc-unit", body).value, servings,
        time_label: $("#sc-time", body).value,
        color: PROFILE_COLORS[data.supplements.length % PROFILE_COLORS.length] };
    };
    $('[data-x="more"]', body).onclick = () => { const v = values(); close(); openSupplementDialog(pid, null, data, v); };
    $(".sheet-form", body).onsubmit = async (e) => {
      e.preventDefault();
      const btn = $('[data-x="add"]', body), err = $(".err", body);
      btn.disabled = true; err.hidden = true;
      try {
        const v = values();
        await api(`/api/profiles/${pid}/supplements`, { method: "POST", body: v });
        changed = true;
        if (navigator.vibrate) navigator.vibrate([20, 40, 20]);
        toast(`${v.name} added`);
        close();
      } catch (x) { err.textContent = x.message.replace(/Value error, /g, ""); err.hidden = false; btn.disabled = false; }
    };
  }

  function showManual() {
    busy = true; clearTimeout(timer);
    showSheet(`<h2 class="sheet-title">Enter the barcode</h2>
      <p class="note">Type the numbers printed under the barcode (8, 12 or 13 digits).</p>
      <form class="sheet-form" autocomplete="off">
        <div class="field"><input id="sc-code" inputmode="numeric" pattern="[0-9 ]{8,16}" maxlength="16" required placeholder="e.g. 0 31604 02616 5" aria-label="Barcode number"></div>
        <div class="sheet-actions"><button type="button" class="btn" data-x="cancel">Cancel</button><button class="btn primary">Look up</button></div>
      </form>`);
    const inp = $("#sc-code", body); setTimeout(() => inp.focus(), 50);
    $('[data-x="cancel"]', body).onclick = () => (stream ? resume() : hideSheet());
    $(".sheet-form", body).onsubmit = (e) => { e.preventDefault(); if (stream) video.pause(); lookup(inp.value); };
  }

  // ---- wiring ----
  $('[data-x="close"]', el).onclick = close;
  $('[data-x="manual"]', el).onclick = showManual;
  $('[data-x="photo"]', el).onclick = () => file.click();
  file.onchange = async () => {
    const f = file.files[0]; file.value = "";
    if (!f) return;
    busy = true; clearTimeout(timer);
    showSheet(`<div class="sheet-loading"><span class="spinner" aria-hidden="true"></span>Reading the barcode…</div>`);
    let code = null;
    try { code = await decodeImage(f); } catch (e) { return showError(e.message, null); }
    if (!code) {
      showSheet(`<h2 class="sheet-title">No barcode found in that photo</h2>
        <p class="note">Get closer so the barcode fills most of the picture, keep it in focus and well lit, or type the numbers instead.</p>
        <div class="sheet-actions"><button class="btn" data-x="manual2">Enter code</button><button class="btn primary" data-x="again">Try another photo</button></div>`);
      $('[data-x="again"]', body).onclick = () => { hideSheet(); file.click(); };
      $('[data-x="manual2"]', body).onclick = showManual;
      return;
    }
    found(code);
  };
  // Tap on the dimmed camera area above the sheet goes back to scanning.
  $(".scan-shade", el).onclick = () => { if (!sheet.hidden && stream) resume(); };

  if (live) startCamera(); else photoMode(null);
}

function fmtCode(c) {
  c = String(c || "");
  if (c.length === 13) return `${c[0]} ${c.slice(1, 7)} ${c.slice(7)}`;
  return c;
}
