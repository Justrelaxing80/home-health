"""SQLite storage. One file under DATA_DIR; a fresh connection per operation."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "home-health.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#2a78d6',
    units       TEXT NOT NULL DEFAULT 'imperial',   -- imperial | metric
    step_goal   INTEGER NOT NULL DEFAULT 10000,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per (profile, provider). Secrets are Fernet-encrypted (see vault.py).
CREATE TABLE IF NOT EXISTS connections (
    profile_id   INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,                      -- garmin | renpho
    email        TEXT NOT NULL,
    password_enc TEXT,
    tokens_enc   TEXT,
    status       TEXT NOT NULL DEFAULT 'linked',     -- linked | syncing | error | needs_relink
    last_sync    TEXT,
    last_error   TEXT,
    PRIMARY KEY (profile_id, provider)
);

-- Garmin daily summary, one row per calendar day.
CREATE TABLE IF NOT EXISTS daily (
    profile_id        INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    date              TEXT NOT NULL,                 -- YYYY-MM-DD
    steps             INTEGER,
    resting_hr        INTEGER,
    avg_stress        INTEGER,
    body_battery_high INTEGER,
    body_battery_low  INTEGER,
    active_kcal       INTEGER,
    total_kcal        INTEGER,
    intensity_min     INTEGER,
    floors            REAL,
    sleep_s           INTEGER,
    deep_s            INTEGER,
    light_s           INTEGER,
    rem_s             INTEGER,
    awake_s           INTEGER,
    sleep_score       INTEGER,
    hrv_avg           INTEGER,
    PRIMARY KEY (profile_id, date)
);

-- Scale readings (Renpho cloud, Renpho CSV, or demo). Weight stored in kg.
CREATE TABLE IF NOT EXISTS body (
    profile_id    INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    measured_at   INTEGER NOT NULL,                  -- unix seconds
    source        TEXT NOT NULL,
    weight_kg     REAL,
    bmi           REAL,
    body_fat      REAL,
    water         REAL,
    muscle        REAL,
    bone          REAL,
    bmr           REAL,
    visceral_fat  REAL,
    subcutaneous_fat REAL,
    protein       REAL,
    metabolic_age REAL,
    PRIMARY KEY (profile_id, measured_at)
);

CREATE TABLE IF NOT EXISTS activities (
    profile_id   INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    activity_id  TEXT NOT NULL,
    start_time   TEXT NOT NULL,                      -- local ISO time from Garmin
    name         TEXT,
    type         TEXT,
    duration_s   REAL,
    distance_m   REAL,
    calories     REAL,
    avg_hr       REAL,
    PRIMARY KEY (profile_id, activity_id)
);

-- Apple Health daily values. Kept apart from Garmin's `daily` so the two never
-- overwrite each other; the dashboard prefers Garmin and fills gaps from Apple.
CREATE TABLE IF NOT EXISTS apple_daily (
    profile_id        INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    date              TEXT NOT NULL,
    steps             INTEGER,
    resting_hr        INTEGER,
    avg_stress        INTEGER,
    body_battery_high INTEGER,
    body_battery_low  INTEGER,
    active_kcal       INTEGER,
    total_kcal        INTEGER,
    intensity_min     INTEGER,
    floors            REAL,
    sleep_s           INTEGER,
    deep_s            INTEGER,
    light_s           INTEGER,
    rem_s             INTEGER,
    awake_s           INTEGER,
    sleep_score       INTEGER,
    hrv_avg           INTEGER,
    PRIMARY KEY (profile_id, date)
);

-- Peptides: one row = a compound + its current vial + its dosing protocol.
-- All peptide times are local wall-clock strings (YYYY-MM-DD / YYYY-MM-DDTHH:MM).
CREATE TABLE IF NOT EXISTS peptides (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id       INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    color            TEXT NOT NULL DEFAULT '#2a78d6',
    vial_mg          REAL,
    water_ml         REAL,
    reconstituted_on TEXT,
    expiry_days      INTEGER NOT NULL DEFAULT 28,
    dose_mcg         REAL NOT NULL,
    dose_unit        TEXT NOT NULL DEFAULT 'mcg',     -- mcg | mg (display only)
    route            TEXT NOT NULL DEFAULT 'SubQ',
    freq             TEXT NOT NULL DEFAULT 'daily',   -- daily | weekdays | interval | as_needed
    weekdays         TEXT NOT NULL DEFAULT '',        -- "0,2,4" (Mon=0)
    interval_days    INTEGER NOT NULL DEFAULT 1,
    times            TEXT NOT NULL DEFAULT '08:00',   -- "08:00,20:00"
    start_date       TEXT NOT NULL,
    end_date         TEXT,
    reminders        INTEGER NOT NULL DEFAULT 1,
    active           INTEGER NOT NULL DEFAULT 1,
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS peptide_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    peptide_id    INTEGER REFERENCES peptides(id) ON DELETE SET NULL,
    peptide_name  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'taken',      -- taken | skipped
    taken_at      TEXT NOT NULL,
    scheduled_for TEXT,
    dose_mcg      REAL,
    units         REAL,
    site          TEXT,
    notes         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS peptide_logs_by_time ON peptide_logs (profile_id, taken_at);

CREATE TABLE IF NOT EXISTS peptide_reminders_sent (
    peptide_id    INTEGER NOT NULL REFERENCES peptides(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,
    PRIMARY KEY (peptide_id, scheduled_for)
);

-- Push reminders via ntfy (https://ntfy.sh or self-hosted).
CREATE TABLE IF NOT EXISTS notify (
    profile_id   INTEGER PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    ntfy_server  TEXT NOT NULL DEFAULT 'https://ntfy.sh',
    ntfy_topic   TEXT NOT NULL,
    generic_text INTEGER NOT NULL DEFAULT 0
);
"""

BODY_FIELDS = (
    "weight_kg", "bmi", "body_fat", "water", "muscle", "bone", "bmr",
    "visceral_fat", "subcutaneous_fat", "protein", "metabolic_age",
)

DAILY_FIELDS = (
    "steps", "resting_hr", "avg_stress", "body_battery_high", "body_battery_low",
    "active_kcal", "total_kcal", "intensity_min", "floors", "sleep_s", "deep_s",
    "light_s", "rem_s", "awake_s", "sleep_score", "hrv_avg",
)


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(sql: str, params=()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def row(sql: str, params=()) -> dict | None:
    with connect() as c:
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else None


def execute(sql: str, params=()) -> int:
    with connect() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def upsert_daily(profile_id: int, date: str, values: dict, table: str = "daily") -> None:
    """Merge non-null values into a day's row; never overwrite data with NULL."""
    if table not in ("daily", "apple_daily"):
        raise ValueError(table)
    vals = {k: v for k, v in values.items() if k in DAILY_FIELDS and v is not None}
    if not vals:
        return
    cols = ", ".join(vals)
    marks = ", ".join("?" for _ in vals)
    updates = ", ".join(f"{k}=excluded.{k}" for k in vals)
    with connect() as c:
        c.execute(
            f"INSERT INTO {table} (profile_id, date, {cols}) VALUES (?, ?, {marks}) "
            f"ON CONFLICT(profile_id, date) DO UPDATE SET {updates}",
            (profile_id, date, *vals.values()),
        )


def upsert_body(profile_id: int, measured_at: int, source: str, values: dict) -> bool:
    vals = {k: v for k, v in values.items() if k in BODY_FIELDS and v is not None}
    if not vals.get("weight_kg"):
        return False
    cols = ", ".join(vals)
    marks = ", ".join("?" for _ in vals)
    updates = ", ".join(f"{k}=excluded.{k}" for k in vals)
    with connect() as c:
        c.execute(
            f"INSERT INTO body (profile_id, measured_at, source, {cols}) "
            f"VALUES (?, ?, ?, {marks}) "
            f"ON CONFLICT(profile_id, measured_at) DO UPDATE SET source=excluded.source, {updates}",
            (profile_id, int(measured_at), source, *vals.values()),
        )
    return True


def upsert_activity(profile_id: int, a: dict) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO activities (profile_id, activity_id, start_time, name, type, "
            "duration_s, distance_m, calories, avg_hr) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(profile_id, activity_id) DO UPDATE SET start_time=excluded.start_time, "
            "name=excluded.name, type=excluded.type, duration_s=excluded.duration_s, "
            "distance_m=excluded.distance_m, calories=excluded.calories, avg_hr=excluded.avg_hr",
            (profile_id, str(a["activity_id"]), a["start_time"], a.get("name"), a.get("type"),
             a.get("duration_s"), a.get("distance_m"), a.get("calories"), a.get("avg_hr")),
        )
