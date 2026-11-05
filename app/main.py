"""Home Health: a household dashboard for Garmin + Renpho data, built for ZimaOS."""

from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import db, demo, garmin, scale, sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


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


class MfaIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


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
    daily = db.rows("SELECT * FROM daily WHERE profile_id=? AND date>=? ORDER BY date", (pid, since))
    body = db.rows("SELECT * FROM body WHERE profile_id=? AND measured_at>=? ORDER BY measured_at",
                   (pid, since_ts))
    latest_body = db.row("SELECT * FROM body WHERE profile_id=? ORDER BY measured_at DESC LIMIT 1", (pid,))
    activities = db.rows(
        "SELECT * FROM activities WHERE profile_id=? ORDER BY start_time DESC LIMIT 8", (pid,))
    return {"profile": p, "connections": _connections(pid), "days": days,
            "daily": daily, "body": body, "latest_body": latest_body, "activities": activities}


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
    if provider not in ("garmin", "renpho"):
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


# ---------------- frontend ----------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
