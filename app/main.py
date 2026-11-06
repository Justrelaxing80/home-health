"""Home Health: a household dashboard for Garmin + Renpho data, built for ZimaOS."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import analysis, apple, barcode, db, demo, garmin, peptides, scale, supplements, sync, tls

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("home-health")
STATIC = Path(__file__).parent / "static"
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MOMENT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_INGEST_BYTES = 64 * 1024 * 1024
MAX_EXPORT_BYTES = 4 * 1024 * 1024 * 1024


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    sync.start_scheduler()
    yield
    sync.stop_scheduler()


app = FastAPI(title="Home Health", lifespan=lifespan)


# ---------------- models ----------------

class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    color: str = "#2a78d6"
    units: str = "imperial"
    step_goal: int = Field(default=10000, ge=1000, le=100000)

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        if not HEX.match(v):
            raise ValueError("color must be #rrggbb")
        return v

    @field_validator("units")
    @classmethod
    def _units(cls, v):
        if v not in ("imperial", "metric"):
            raise ValueError("units must be imperial or metric")
        return v


class LinkIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class KeyIn(BaseModel):
    api_key: str = Field(min_length=20, max_length=4000)


class MfaIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class PeptideIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: str = "#2a78d6"
    vial_mg: float | None = Field(default=None, gt=0, le=100000)
    water_ml: float | None = Field(default=None, gt=0, le=100)
    reconstituted_on: str | None = None
    expiry_days: int = Field(default=28, ge=1, le=365)
    dose_mcg: float = Field(gt=0, le=10_000_000)
    dose_unit: Literal["mcg", "mg"] = "mcg"
    route: Literal["SubQ", "IM", "Oral", "Nasal", "Topical", "Other"] = "SubQ"
    freq: Literal["daily", "weekdays", "interval", "as_needed"] = "daily"
    weekdays: str = ""
    interval_days: int = Field(default=1, ge=1, le=90)
    times: str = "08:00"
    start_date: str
    end_date: str | None = None
    reminders: bool = True
    active: bool = True
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _check(self):
        if not HEX.match(self.color):
            raise ValueError("color must be #rrggbb")
        self.name = self.name.strip()
        for d in (self.start_date, self.reconstituted_on, self.end_date):
            if d and not DAY.match(d):
                raise ValueError("dates must be YYYY-MM-DD")
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end date is before the start date")
        times = [t.strip() for t in self.times.split(",") if t.strip()]
        if self.freq != "as_needed" and not times:
            raise ValueError("add at least one dose time")
        if any(not HHMM.match(t) for t in times) or len(times) > 6:
            raise ValueError("dose times must be HH:MM (up to 6)")
        self.times = ",".join(sorted(set(times)))
        days = sorted({w.strip() for w in self.weekdays.split(",") if w.strip()})
        if any(len(w) != 1 or w not in "0123456" for w in days):
            raise ValueError("weekdays must be 0-6")
        if self.freq == "weekdays" and not days:
            raise ValueError("pick at least one weekday")
        self.weekdays = ",".join(days)
        return self


class DoseLogIn(BaseModel):
    peptide_id: int
    status: Literal["taken", "skipped"] = "taken"
    taken_at: str
    scheduled_for: str | None = None
    dose_mcg: float | None = Field(default=None, gt=0, le=10_000_000)
    site: str | None = Field(default=None, max_length=40)
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _check(self):
        for v in (self.taken_at, self.scheduled_for):
            if v and not MOMENT.match(v):
                raise ValueError("times must be YYYY-MM-DDTHH:MM")
        return self


class NotifyIn(BaseModel):
    ntfy_server: str = Field(default="https://ntfy.sh", max_length=200)
    ntfy_topic: str = Field(min_length=6, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    generic_text: bool = False

    @field_validator("ntfy_server")
    @classmethod
    def _server(cls, v):
        if not re.match(r"^https?://[^\s/]+(/\S*)?$", v):
            raise ValueError("server must be an http(s) URL")
        return v.rstrip("/")


class SupplementIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: str = "#1baf7a"
    amount: float | None = Field(default=None, gt=0, le=1_000_000)
    unit: str = Field(default="", max_length=20)
    servings: int = Field(default=1, ge=1, le=6)
    freq: Literal["daily", "weekdays"] = "daily"
    weekdays: str = ""
    time_label: str = Field(default="", max_length=30)
    remind_times: str = ""
    start_date: str | None = None
    active: bool = True
    notes: str = Field(default="", max_length=500)
    barcode: str = Field(default="", max_length=20)
    brand: str = Field(default="", max_length=60)
    image_url: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _check(self):
        if not HEX.match(self.color):
            raise ValueError("color must be #rrggbb")
        self.name = self.name.strip()
        if self.barcode:
            code = barcode.normalize(self.barcode)
            if not code:
                raise ValueError("barcode isn't a valid UPC/EAN")
            self.barcode = code
        self.brand = self.brand.strip()
        if self.image_url and not self.image_url.startswith("https://"):
            self.image_url = ""
        if self.start_date and not DAY.match(self.start_date):
            raise ValueError("start date must be YYYY-MM-DD")
        days = sorted({w.strip() for w in self.weekdays.split(",") if w.strip()})
        if any(len(w) != 1 or w not in "0123456" for w in days):
            raise ValueError("weekdays must be 0-6")
        if self.freq == "weekdays" and not days:
            raise ValueError("pick at least one weekday")
        self.weekdays = ",".join(days)
        times = sorted({t.strip() for t in self.remind_times.split(",") if t.strip()})
        if any(not HHMM.match(t) for t in times) or len(times) > 6:
            raise ValueError("reminder times must be HH:MM (up to 6)")
        self.remind_times = ",".join(times)
        return self


class ServingIn(BaseModel):
    date: str
    serving: int = Field(ge=1, le=6)
    taken: bool = True

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if not DAY.match(v):
            raise ValueError("date must be YYYY-MM-DD")
        return v


def _profile(pid: int) -> dict:
    p = db.row("SELECT * FROM profiles WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "Profile not found")
    return p


def _connections(pid: int) -> list[dict]:
    return db.rows(
        "SELECT provider, email, status, last_sync, last_error FROM connections WHERE profile_id=?",
        (pid,),
    )


# ---------------- profiles ----------------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/profiles")
def list_profiles():
    profiles = db.rows("SELECT * FROM profiles ORDER BY id")
    for p in profiles:
        p["connections"] = [c["provider"] for c in _connections(p["id"])]
    return profiles


@app.post("/api/profiles", status_code=201)
def create_profile(body: ProfileIn):
    pid = db.execute(
        "INSERT INTO profiles (name, color, units, step_goal) VALUES (?, ?, ?, ?)",
        (body.name.strip(), body.color, body.units, body.step_goal),
    )
    return _profile(pid)


@app.put("/api/profiles/{pid}")
def update_profile(pid: int, body: ProfileIn):
    _profile(pid)
    db.execute(
        "UPDATE profiles SET name=?, color=?, units=?, step_goal=? WHERE id=?",
        (body.name.strip(), body.color, body.units, body.step_goal, pid),
    )
    return _profile(pid)


@app.delete("/api/profiles/{pid}", status_code=204)
def delete_profile(pid: int):
    _profile(pid)
    db.execute("DELETE FROM profiles WHERE id=?", (pid,))


@app.post("/api/demo", status_code=201)
def create_demo():
    return _profile(demo.create_demo_profile())


# ---------------- dashboard data ----------------

@app.get("/api/profiles/{pid}/dashboard")
def dashboard(pid: int, days: int = 90):
    days = max(7, min(days, 730))
    p = _profile(pid)
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    since_ts = int(time.time()) - days * 86400
    daily = _merged_daily(pid, since)
    body = db.rows("SELECT * FROM body WHERE profile_id=? AND measured_at>=? ORDER BY measured_at",
                   (pid, since_ts))
    latest_body = db.row("SELECT * FROM body WHERE profile_id=? ORDER BY measured_at DESC LIMIT 1", (pid,))
    activities = db.rows(
        "SELECT * FROM activities WHERE profile_id=? ORDER BY start_time DESC LIMIT 8", (pid,))
    pep = peptides.summary(pid)
    supp = supplements.summary(pid)
    return {"profile": p, "connections": _connections(pid), "days": days,
            "daily": daily, "body": body, "latest_body": latest_body, "activities": activities,
            "peptides": pep, "supplements": supp, "analysis": _analysis(p, pep, supp)}


def _analysis(p: dict, pep, supp) -> dict | None:
    today = peptides.now_local().date()
    since = (today - timedelta(days=40)).isoformat()
    since_ts = int(time.time()) - 31 * 86400
    body = db.rows("SELECT * FROM body WHERE profile_id=? AND measured_at>=? ORDER BY measured_at",
                   (p["id"], since_ts))
    acts = db.rows("SELECT * FROM activities WHERE profile_id=? AND start_time>=? ORDER BY start_time",
                   (p["id"], (today - timedelta(days=30)).isoformat()))
    return analysis.build(p, _merged_daily(p["id"], since), body, acts, supp, pep)


def _merged_daily(pid: int, since: str) -> list[dict]:
    """Garmin values win; Apple Health fills whatever Garmin doesn't have."""
    merged: dict[str, dict] = {}
    for r in db.rows("SELECT * FROM apple_daily WHERE profile_id=? AND date>=?", (pid, since)):
        merged[r["date"]] = r
    for r in db.rows("SELECT * FROM daily WHERE profile_id=? AND date>=?", (pid, since)):
        base = merged.get(r["date"], {})
        merged[r["date"]] = {**base, **{k: v for k, v in r.items() if v is not None}}
    return [merged[d] for d in sorted(merged)]


# ---------------- connections ----------------

@app.get("/api/profiles/{pid}/connections")
def connections(pid: int):
    _profile(pid)
    return _connections(pid)


@app.post("/api/profiles/{pid}/garmin/link")
def garmin_link(pid: int, body: LinkIn):
    _profile(pid)
    try:
        status = garmin.link(pid, body.email.strip(), body.password)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Garmin login failed: {e}") from e
    if status == "linked":
        sync.start(pid, "garmin")
    return {"status": status}


@app.post("/api/profiles/{pid}/garmin/link-key")
def garmin_link_key(pid: int, body: KeyIn):
    _profile(pid)
    try:
        garmin.link_key(pid, body.api_key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Garmin key not accepted: {e}") from e
    sync.start(pid, "garmin")
    return {"status": "linked"}


@app.post("/api/profiles/{pid}/garmin/mfa")
def garmin_mfa(pid: int, body: MfaIn):
    _profile(pid)
    try:
        garmin.submit_mfa(pid, body.code)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Garmin MFA failed: {e}") from e
    sync.start(pid, "garmin")
    return {"status": "linked"}


@app.post("/api/profiles/{pid}/renpho/link")
def renpho_link(pid: int, body: LinkIn):
    _profile(pid)
    try:
        scale.link(pid, body.email.strip(), body.password)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Renpho login failed: {e}") from e
    sync.start(pid, "renpho")
    return {"status": "linked"}


@app.delete("/api/profiles/{pid}/{provider}", status_code=204)
def unlink(pid: int, provider: str):
    if provider not in ("garmin", "renpho", "apple"):
        raise HTTPException(404)
    db.execute("DELETE FROM connections WHERE profile_id=? AND provider=?", (pid, provider))


@app.post("/api/profiles/{pid}/sync")
def sync_now(pid: int):
    _profile(pid)
    return {"queued": sync.start(pid)}


@app.post("/api/profiles/{pid}/import/renpho-csv")
async def import_renpho_csv(pid: int, file: UploadFile = File(...)):
    _profile(pid)
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large (10 MB max)")
    try:
        return scale.import_csv(pid, data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---------------- Apple Health ----------------

@app.get("/api/profiles/{pid}/apple")
def apple_info(pid: int):
    _profile(pid)
    return {"token": apple.token_for(pid)}


@app.post("/api/profiles/{pid}/apple/enable")
def apple_enable(pid: int):
    _profile(pid)
    return {"token": apple.enable(pid)}


@app.post("/api/ingest/apple/{token}")
async def apple_ingest(token: str, request: Request):
    pid = apple.profile_for_token(token)
    if not pid:
        raise HTTPException(404, "Unknown ingest URL")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_INGEST_BYTES:
            raise HTTPException(413, "Payload too large; export fewer days at a time")
    try:
        payload = json.loads(bytes(raw) or b"{}")
    except ValueError as e:
        raise HTTPException(400, "Body must be JSON from Health Auto Export") from e
    return apple.ingest_json(pid, payload)


@app.post("/api/profiles/{pid}/import/apple-export", status_code=202)
async def import_apple_export(pid: int, file: UploadFile = File(...)):
    _profile(pid)
    tmp = db.DATA_DIR / f"apple-import-{pid}-{secrets.token_hex(4)}.tmp"
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > MAX_EXPORT_BYTES:
                    raise HTTPException(413, "File too large (4 GB max)")
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    def work():
        try:
            apple.import_export(pid, str(tmp))
        except Exception:  # noqa: BLE001 - already logged and stored on the connection
            pass
        finally:
            tmp.unlink(missing_ok=True)

    apple.mark_importing(pid)
    sync.submit(work)
    return {"queued": True, "bytes": size}


# ---------------- peptides ----------------

PEPTIDE_COLS = ("name", "color", "vial_mg", "water_ml", "reconstituted_on", "expiry_days", "dose_mcg",
                "dose_unit", "route", "freq", "weekdays", "interval_days", "times", "start_date",
                "end_date", "reminders", "active", "notes")


def _peptide(pid: int, peptide_id: int) -> dict:
    p = peptides.get(pid, peptide_id)
    if not p:
        raise HTTPException(404, "Peptide not found")
    return p


@app.get("/api/profiles/{pid}/peptides")
def peptide_list(pid: int):
    _profile(pid)
    return {"peptides": peptides.list_peptides(pid), "sites": peptides.SITES,
            "now": peptides.now_local().strftime("%Y-%m-%dT%H:%M")}


@app.post("/api/profiles/{pid}/peptides", status_code=201)
def peptide_create(pid: int, body: PeptideIn):
    _profile(pid)
    vals = body.model_dump()
    new_id = db.execute(
        f"INSERT INTO peptides (profile_id, {', '.join(PEPTIDE_COLS)}) "
        f"VALUES (?, {', '.join('?' for _ in PEPTIDE_COLS)})",
        (pid, *(vals[c] for c in PEPTIDE_COLS)))
    return _peptide(pid, new_id)


@app.put("/api/profiles/{pid}/peptides/{peptide_id}")
def peptide_update(pid: int, peptide_id: int, body: PeptideIn):
    _peptide(pid, peptide_id)
    vals = body.model_dump()
    db.execute(f"UPDATE peptides SET {', '.join(c + '=?' for c in PEPTIDE_COLS)} WHERE id=?",
               (*(vals[c] for c in PEPTIDE_COLS), peptide_id))
    return _peptide(pid, peptide_id)


@app.delete("/api/profiles/{pid}/peptides/{peptide_id}", status_code=204)
def peptide_delete(pid: int, peptide_id: int):
    _peptide(pid, peptide_id)
    db.execute("DELETE FROM peptides WHERE id=?", (peptide_id,))


@app.post("/api/profiles/{pid}/peptides/{peptide_id}/new-vial")
def peptide_new_vial(pid: int, peptide_id: int):
    _peptide(pid, peptide_id)
    db.execute("UPDATE peptides SET reconstituted_on=? WHERE id=?",
               (peptides.now_local().date().isoformat(), peptide_id))
    return _peptide(pid, peptide_id)


@app.get("/api/profiles/{pid}/peptides/day")
def peptide_day(pid: int, day: str | None = None):
    _profile(pid)
    if day and not DAY.match(day):
        raise HTTPException(422, "day must be YYYY-MM-DD")
    return peptides.day_view(pid, date.fromisoformat(day) if day else peptides.now_local().date())


@app.get("/api/profiles/{pid}/peptides/history")
def peptide_history(pid: int, days: int = 30):
    _profile(pid)
    return peptides.history(pid, max(7, min(days, 365)))


@app.post("/api/profiles/{pid}/peptide-logs", status_code=201)
def dose_log(pid: int, body: DoseLogIn):
    p = _peptide(pid, body.peptide_id)
    taken = body.status == "taken"
    dose = body.dose_mcg or p["dose_mcg"]
    with db.connect() as c:
        if body.scheduled_for:  # re-logging a slot replaces the earlier entry
            c.execute("DELETE FROM peptide_logs WHERE peptide_id=? AND scheduled_for=?",
                      (p["id"], body.scheduled_for))
        cur = c.execute(
            "INSERT INTO peptide_logs (profile_id, peptide_id, peptide_name, status, taken_at, "
            "scheduled_for, dose_mcg, units, site, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, p["id"], p["name"], body.status, body.taken_at, body.scheduled_for,
             dose if taken else None, peptides.units_for(p, dose) if taken else None,
             (body.site or None) if taken else None, body.notes))
        log_id = cur.lastrowid
    return db.row("SELECT * FROM peptide_logs WHERE id=?", (log_id,))


@app.delete("/api/profiles/{pid}/peptide-logs/{log_id}", status_code=204)
def dose_log_delete(pid: int, log_id: int):
    if not db.row("SELECT 1 FROM peptide_logs WHERE id=? AND profile_id=?", (log_id, pid)):
        raise HTTPException(404, "Log entry not found")
    db.execute("DELETE FROM peptide_logs WHERE id=?", (log_id,))


# ---------------- supplements ----------------

SUPP_COLS = ("name", "color", "amount", "unit", "servings", "freq", "weekdays", "time_label",
             "remind_times", "start_date", "active", "notes", "barcode", "brand", "image_url")


def _supplement(pid: int, sid: int) -> dict:
    s = supplements.get(pid, sid)
    if not s:
        raise HTTPException(404, "Supplement not found")
    return s


@app.get("/api/profiles/{pid}/barcode/{code}")
def barcode_lookup(pid: int, code: str):
    """Product details for a scanned barcode, plus this profile's supplement with that barcode."""
    _profile(pid)
    if len(code) > 20:
        raise HTTPException(422, "barcode too long")
    r = barcode.lookup(code)
    mine = db.row("SELECT id FROM supplements WHERE profile_id=? AND barcode=? ORDER BY active DESC, id DESC",
                  (pid, r["code"])) if r.get("code") else None
    return {**r, "existing_id": mine["id"] if mine else None}


@app.get("/api/products/search")
def product_search(q: str = ""):
    """Find a product by name when its barcode isn't in any database."""
    return barcode.search(q[:80])


@app.get("/api/products/{source}/{ref}")
def product_detail(source: Literal["dsld", "off"], ref: str):
    if not re.fullmatch(r"\d{1,14}", ref):
        raise HTTPException(422, "bad product reference")
    try:
        p = barcode.product(source, ref)
    except Exception as e:  # noqa: BLE001
        log.warning("product %s/%s failed: %s", source, ref, e)
        raise HTTPException(502, "Couldn't reach the product database. Try again.") from e
    if not p:
        raise HTTPException(404, "Product not found")
    return {"found": True, **p}


# ---------------- https for the live scanner ----------------

@app.middleware("http")
async def _note_host(request: Request, call_next):
    # Every address the app is opened at (Tailscale IP, LAN IP, ...) goes on its https certificate.
    if tls.ACTIVE:
        tls.note_host(request.headers.get("host", ""))
    return await call_next(request)


@app.get("/api/tls")
def tls_info(request: Request):
    return {**tls.info(), "secure": request.url.scheme == "https"}


@app.get("/api/tls/ca.crt")
def tls_ca():
    if not tls.ACTIVE:
        raise HTTPException(404, "https isn't enabled")
    # No Content-Disposition: iOS Safari then offers to install it as a profile.
    return Response(tls.ca_der(), media_type="application/x-x509-ca-cert")


@app.get("/api/profiles/{pid}/supplements")
def supplement_page(pid: int, day: str | None = None):
    _profile(pid)
    if day and not DAY.match(day):
        raise HTTPException(422, "day must be YYYY-MM-DD")
    return supplements.page(pid, date.fromisoformat(day) if day else supplements.today())


@app.post("/api/profiles/{pid}/supplements", status_code=201)
def supplement_create(pid: int, body: SupplementIn):
    _profile(pid)
    vals = body.model_dump()
    vals["start_date"] = vals["start_date"] or supplements.today().isoformat()
    new_id = db.execute(
        f"INSERT INTO supplements (profile_id, {', '.join(SUPP_COLS)}) VALUES (?, {', '.join('?' for _ in SUPP_COLS)})",
        (pid, *(vals[c] for c in SUPP_COLS)))
    return _supplement(pid, new_id)


@app.put("/api/profiles/{pid}/supplements/{sid}")
def supplement_update(pid: int, sid: int, body: SupplementIn):
    cur = _supplement(pid, sid)
    vals = body.model_dump()
    vals["start_date"] = vals["start_date"] or cur["start_date"]
    db.execute(f"UPDATE supplements SET {', '.join(c + '=?' for c in SUPP_COLS)} WHERE id=?",
               (*(vals[c] for c in SUPP_COLS), sid))
    # Servings above a lowered daily count no longer apply.
    db.execute("DELETE FROM supplement_logs WHERE supplement_id=? AND serving>?", (sid, vals["servings"]))
    return _supplement(pid, sid)


@app.delete("/api/profiles/{pid}/supplements/{sid}", status_code=204)
def supplement_delete(pid: int, sid: int):
    _supplement(pid, sid)
    db.execute("DELETE FROM supplements WHERE id=?", (sid,))


@app.put("/api/profiles/{pid}/supplements/{sid}/serving")
def supplement_serving(pid: int, sid: int, body: ServingIn):
    s = _supplement(pid, sid)
    if body.serving > s["servings"]:
        raise HTTPException(422, f"{s['name']} has {s['servings']} serving(s) a day")
    if body.date > supplements.today().isoformat():
        raise HTTPException(422, "Can't log a future day")
    if body.taken:
        db.execute("INSERT OR IGNORE INTO supplement_logs (supplement_id, profile_id, date, serving, taken_at) "
                   "VALUES (?,?,?,?,?)", (sid, pid, body.date, body.serving,
                                          peptides.now_local().strftime("%Y-%m-%dT%H:%M")))
    else:
        db.execute("DELETE FROM supplement_logs WHERE supplement_id=? AND date=? AND serving=?",
                   (sid, body.date, body.serving))
    return {"taken": db.row("SELECT COUNT(*) AS n FROM supplement_logs WHERE supplement_id=? AND date=?",
                            (sid, body.date))["n"]}


@app.get("/api/profiles/{pid}/notify")
def notify_get(pid: int):
    _profile(pid)
    return db.row("SELECT * FROM notify WHERE profile_id=?", (pid,))


@app.put("/api/profiles/{pid}/notify")
def notify_put(pid: int, body: NotifyIn):
    _profile(pid)
    db.execute("INSERT INTO notify (profile_id, ntfy_server, ntfy_topic, generic_text) VALUES (?,?,?,?) "
               "ON CONFLICT(profile_id) DO UPDATE SET ntfy_server=excluded.ntfy_server, "
               "ntfy_topic=excluded.ntfy_topic, generic_text=excluded.generic_text",
               (pid, body.ntfy_server, body.ntfy_topic, int(body.generic_text)))
    return db.row("SELECT * FROM notify WHERE profile_id=?", (pid,))


@app.delete("/api/profiles/{pid}/notify", status_code=204)
def notify_delete(pid: int):
    db.execute("DELETE FROM notify WHERE profile_id=?", (pid,))


@app.post("/api/profiles/{pid}/notify/test")
def notify_test(pid: int):
    cfg = db.row("SELECT * FROM notify WHERE profile_id=?", (pid,))
    if not cfg:
        raise HTTPException(400, "Save reminder settings first")
    try:
        peptides.send_ntfy(cfg["ntfy_server"], cfg["ntfy_topic"], "Home Health",
                           "Test reminder - notifications are working.", tags="white_check_mark")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't reach {cfg['ntfy_server']}: {e}") from e
    return {"sent": True}


# ---------------- frontend ----------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
