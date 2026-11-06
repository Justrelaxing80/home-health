"""Garmin Connect link + sync via the unofficial `garminconnect` library.

Linking: email/password login; if Garmin asks for an MFA code the half-finished
login is parked in memory until the user submits the code from the web UI.
Sync: restore the saved session tokens (refreshing them as needed), pull daily
stats, sleep, HRV, activities and scale weigh-ins (e.g. a Renpho scale linked
to Garmin Connect), then persist the possibly-refreshed tokens.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

from garminconnect import Garmin

from . import db, vault

log = logging.getLogger("home-health.garmin")

BACKFILL_DAYS = int(os.environ.get("GARMIN_BACKFILL_DAYS", "30"))
REFRESH_DAYS = 3          # recent days are re-pulled because Garmin revises them
WEIGHT_BACKFILL_DAYS = 365
WEIGHT_REFRESH_DAYS = 14
MFA_TIMEOUT_S = 600

# profile_id -> (Garmin instance, client_state, email, password, created)
_pending_mfa: dict[int, tuple] = {}
_pending_lock = threading.Lock()


class NeedsMfa(Exception):
    pass


def _save_link(profile_id: int, email: str, password: str, g: Garmin) -> None:
    tokens = g.client.dumps()
    with db.connect() as c:
        c.execute(
            "INSERT INTO connections (profile_id, provider, email, password_enc, tokens_enc, status, last_error) "
            "VALUES (?, 'garmin', ?, ?, ?, 'linked', NULL) "
            "ON CONFLICT(profile_id, provider) DO UPDATE SET email=excluded.email, "
            "password_enc=excluded.password_enc, tokens_enc=excluded.tokens_enc, "
            "status='linked', last_error=NULL",
            (profile_id, email, vault.encrypt(password), vault.encrypt(tokens)),
        )


def link(profile_id: int, email: str, password: str) -> str:
    """Returns 'linked' or 'needs_mfa'. Raises on bad credentials."""
    g = Garmin(email, password, return_on_mfa=True)
    status, state = g.login()
    if status == "needs_mfa":
        with _pending_lock:
            _pending_mfa[profile_id] = (g, state, email, password, time.time())
        return "needs_mfa"
    _save_link(profile_id, email, password, g)
    return "linked"


def submit_mfa(profile_id: int, code: str) -> None:
    with _pending_lock:
        pending = _pending_mfa.pop(profile_id, None)
    if not pending or time.time() - pending[4] > MFA_TIMEOUT_S:
        raise ValueError("No Garmin login is waiting for a code (or it expired). Start linking again.")
    g, state, email, password, _ = pending
    g.resume_login(state, code.strip())
    _save_link(profile_id, email, password, g)


def _client_for(profile_id: int) -> Garmin:
    conn = db.row("SELECT * FROM connections WHERE profile_id=? AND provider='garmin'", (profile_id,))
    if not conn:
        raise LookupError("Garmin is not linked")
    password = vault.decrypt(conn["password_enc"])
    tokens = vault.decrypt(conn["tokens_enc"])
    g = Garmin(conn["email"], password)
    try:
        g.login(tokenstore=tokens) if tokens else g.login()
    except Exception as e:  # noqa: BLE001
        # Most likely: tokens expired and the fallback password login hit an MFA prompt.
        raise NeedsMfa(str(e)) from e
    return g


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _day(g: Garmin, profile_id: int, d: str) -> None:
    values: dict = {}
    try:
        s = g.get_stats(d) or {}
        mod = _num(s.get("moderateIntensityMinutes")) or 0
        vig = _num(s.get("vigorousIntensityMinutes")) or 0
        values.update(
            steps=_num(s.get("totalSteps")),
            resting_hr=_num(s.get("restingHeartRate")),
            avg_stress=(lambda x: x if x is not None and x >= 0 else None)(_num(s.get("averageStressLevel"))),
            body_battery_high=_num(s.get("bodyBatteryHighestValue")),
            body_battery_low=_num(s.get("bodyBatteryLowestValue")),
            active_kcal=_num(s.get("activeKilocalories")),
            total_kcal=_num(s.get("totalKilocalories")),
            intensity_min=(mod + 2 * vig) if (mod or vig) else None,  # Garmin counts vigorous x2
            floors=_num(s.get("floorsAscended")),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("stats %s failed: %s", d, e)

    try:
        sl = (g.get_sleep_data(d) or {}).get("dailySleepDTO") or {}
        score = ((sl.get("sleepScores") or {}).get("overall") or {}).get("value")
        values.update(
            sleep_s=_num(sl.get("sleepTimeSeconds")),
            deep_s=_num(sl.get("deepSleepSeconds")),
            light_s=_num(sl.get("lightSleepSeconds")),
            rem_s=_num(sl.get("remSleepSeconds")),
            awake_s=_num(sl.get("awakeSleepSeconds")),
            sleep_score=_num(score),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("sleep %s failed: %s", d, e)

    try:
        h = (g.get_hrv_data(d) or {}).get("hrvSummary") or {}
        values["hrv_avg"] = _num(h.get("lastNightAvg"))
    except Exception as e:  # noqa: BLE001
        log.debug("hrv %s unavailable: %s", d, e)  # many watches don't record HRV

    db.upsert_daily(profile_id, d, values)


def sync(profile_id: int) -> dict:
    g = _client_for(profile_id)

    have = db.row("SELECT MAX(date) AS d FROM daily WHERE profile_id=?", (profile_id,))
    today = date.today()
    if have and have["d"]:
        start = min(date.fromisoformat(have["d"]), today - timedelta(days=REFRESH_DAYS - 1))
    else:
        start = today - timedelta(days=BACKFILL_DAYS - 1)

    days = 0
    d = start
    while d <= today:
        _day(g, profile_id, d.isoformat())
        days += 1
        d += timedelta(days=1)

    acts = 0
    try:
        for a in g.get_activities_by_date(start.isoformat(), today.isoformat()) or []:
            db.upsert_activity(profile_id, {
                "activity_id": a.get("activityId"),
                "start_time": a.get("startTimeLocal") or a.get("startTimeGMT") or "",
                "name": a.get("activityName"),
                "type": (a.get("activityType") or {}).get("typeKey"),
                "duration_s": a.get("duration"),
                "distance_m": a.get("distance"),
                "calories": a.get("calories"),
                "avg_hr": a.get("averageHR"),
            })
            acts += 1
    except Exception as e:  # noqa: BLE001
        log.warning("activities failed: %s", e)

    weighins = 0
    try:
        weighins = _weigh_ins(g, profile_id, today)
    except Exception as e:  # noqa: BLE001
        log.warning("weigh-ins failed: %s", e)

    # Persist refreshed tokens so the next run doesn't need the password/MFA.
    with db.connect() as c:
        c.execute(
            "UPDATE connections SET tokens_enc=?, last_sync=? WHERE profile_id=? AND provider='garmin'",
            (vault.encrypt(g.client.dumps()), datetime.now(timezone.utc).isoformat(timespec="seconds"), profile_id),
        )
    return {"days": days, "activities": acts, "weigh_ins": weighins}


def parse_weigh_in(w: dict) -> tuple[int, dict] | None:
    """One Garmin `dateWeightList` entry -> (unix seconds, body values). Weights come in grams.

    Only fields with the same meaning as the Renpho ones are kept (muscle and bone
    arrive as masses, while the app stores Renpho's percentages, so they're skipped).
    """
    grams = _num(w.get("weight"))
    ms = _num(w.get("timestampGMT")) or _num(w.get("date"))
    if not grams or not ms:
        return None
    vals = {
        "weight_kg": round(grams / 1000, 2),
        "bmi": _num(w.get("bmi")),
        "body_fat": _num(w.get("bodyFat")),
        "water": _num(w.get("bodyWater")),
        "visceral_fat": _num(w.get("visceralFat")),
        "metabolic_age": _num(w.get("metabolicAge")),
    }
    return int(ms // 1000), {k: v for k, v in vals.items() if v is not None}


def _weigh_ins(g: Garmin, profile_id: int, today: date) -> int:
    have = db.row("SELECT 1 FROM body WHERE profile_id=? AND source='garmin' LIMIT 1", (profile_id,))
    start = today - timedelta(days=(WEIGHT_REFRESH_DAYS if have else WEIGHT_BACKFILL_DAYS) - 1)
    n = 0
    while start <= today:
        end = min(today, start + timedelta(days=89))
        data = g.get_body_composition(start.isoformat(), end.isoformat()) or {}
        for w in data.get("dateWeightList") or []:
            parsed = parse_weigh_in(w)
            if not parsed:
                continue
            ts, vals = parsed
            # The same weigh-in may already be here from the Renpho link or a CSV import.
            if db.row("SELECT 1 FROM body WHERE profile_id=? AND source<>'garmin' AND ABS(measured_at-?)<600",
                      (profile_id, ts)):
                continue
            if db.upsert_body(profile_id, ts, "garmin", vals):
                n += 1
        start = end + timedelta(days=1)
    # And if Renpho/CSV delivered a reading after Garmin did, keep theirs (more fields).
    db.execute("DELETE FROM body WHERE profile_id=? AND source='garmin' AND EXISTS (SELECT 1 FROM body b2 "
               "WHERE b2.profile_id=body.profile_id AND b2.source<>'garmin' "
               "AND ABS(b2.measured_at-body.measured_at)<600)", (profile_id,))
    return n
