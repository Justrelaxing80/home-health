"""Garmin Connect link + sync.

Two ways to link:
- A Garmin Connect Personal API key (official; created at
  connect.garmin.com/app/settings/personalApiToken). The key is exchanged for a
  24-hour bearer token at personal.connectapi.garmin.com. Preferred: no password,
  no MFA prompts.
- Email/password via the unofficial `garminconnect` library.

Password linking: email/password login; if Garmin asks for an MFA code the half-finished
login is parked in memory until the user submits the code from the web UI.
Sync: restore the saved session tokens (refreshing them as needed), pull daily
stats, sleep, HRV, activities and scale weigh-ins (e.g. a Renpho scale linked
to Garmin Connect), then persist the possibly-refreshed tokens.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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


class KeyRejected(Exception):
    """Garmin refused the Personal API key (expired, deleted, or mistyped)."""


# ---------------- official Personal API ----------------

PERSONAL_API_BASE = os.environ.get("GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
PERSONAL_KIND = "personal_api"


def _jwt_exp(token: str) -> float:
    try:
        part = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"])
    except Exception as e:  # noqa: BLE001
        raise KeyRejected("Garmin's token endpoint didn't return a valid token.") from e


class PersonalApi:
    """Duck-types the few `garminconnect.Garmin` methods sync() uses, backed by the
    official Personal API (same response shapes as Garmin Connect's own endpoints)."""

    def __init__(self, key: str, token: str | None = None, exp: float = 0):
        self.key, self.token, self.exp = key.strip(), token, exp
        self.client = self  # sync() persists state via g.client.dumps()

    def dumps(self) -> str:
        return json.dumps({"kind": PERSONAL_KIND, "token": self.token, "exp": self.exp})

    def _request(self, method: str, path: str, headers: dict, timeout: float = 30):
        req = urllib.request.Request(PERSONAL_API_BASE + path, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https host)
            return r.status, r.read()

    def refresh_token(self, force: bool = False) -> None:
        if not force and self.token and self.exp - time.time() > 3600:
            return
        try:
            _, body = self._request("POST", TOKEN_PATH, {"Accept": "*/*", "Authorization": f"ApiKey {self.key}"})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise KeyRejected(f"Garmin rejected the Personal API key (HTTP {e.code}). It may be expired, "
                                  "deleted, or copied incompletely. Create a new key and link again.") from e
            raise
        self.token = body.decode().strip()
        self.exp = _jwt_exp(self.token)

    def get(self, path: str, params: dict | None = None):
        self.refresh_token()
        if params:
            path += "?" + urllib.parse.urlencode(params)
        hdr = lambda: {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}  # noqa: E731
        try:
            status, body = self._request("GET", path, hdr())
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self.refresh_token(force=True)  # token revoked early: get a fresh one once
            status, body = self._request("GET", path, hdr())
        return json.loads(body) if status != 204 and body.strip() else None

    # --- the garminconnect-compatible subset ---
    def get_stats(self, d: str):
        return self.get("/usersummary-service/usersummary/daily", {"calendarDate": d})

    def get_sleep_data(self, d: str):
        s = self.get("/sleep-service/sleep/dailySleep", {"date": d}) or {}
        return s if "dailySleepDTO" in s else {"dailySleepDTO": s}

    def get_hrv_data(self, d: str):
        return self.get(f"/hrv-service/hrv/{d}")

    def get_activities_by_date(self, start: str, end: str):
        out, offset = [], 0
        while offset < 2000:
            page = self.get("/activitylist-service/activities/search/activities",
                            {"start": offset, "limit": 99, "startDate": start, "endDate": end}) or []
            out += page
            if len(page) < 99:
                break
            offset += 99
        return out

    def get_body_composition(self, start: str, end: str):
        r = self.get(f"/weight-service/weight/range/{start}/{end}", {"includeAll": "true"}) or {}
        return {"dateWeightList": [w for day in r.get("dailyWeightSummaries") or []
                                   for w in day.get("allWeightMetrics") or []]}

    def display_name(self) -> str:
        s = self.get("/userprofile-service/userprofile/settings") or {}
        return s.get("displayName") or s.get("userName") or ""


def link_key(profile_id: int, key: str) -> None:
    """Validate a Personal API key (token exchange + one data call) and save it."""
    api = PersonalApi(key)
    api.refresh_token(force=True)
    try:
        name = api.display_name()
    except urllib.error.HTTPError as e:
        raise KeyRejected(f"The key was accepted, but reading your profile failed (HTTP {e.code}).") from e
    label = f"Personal API key{f' ({name})' if name else ''}"
    with db.connect() as c:
        c.execute(
            "INSERT INTO connections (profile_id, provider, email, password_enc, tokens_enc, status, last_error) "
            "VALUES (?, 'garmin', ?, ?, ?, 'linked', NULL) "
            "ON CONFLICT(profile_id, provider) DO UPDATE SET email=excluded.email, "
            "password_enc=excluded.password_enc, tokens_enc=excluded.tokens_enc, "
            "status='linked', last_error=NULL",
            (profile_id, label, vault.encrypt(api.key), vault.encrypt(api.dumps())),
        )


def _personal_state(tokens: str | None) -> dict | None:
    try:
        st = json.loads(tokens or "")
    except ValueError:
        return None
    return st if isinstance(st, dict) and st.get("kind") == PERSONAL_KIND else None


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


def _client_for(profile_id: int):
    conn = db.row("SELECT * FROM connections WHERE profile_id=? AND provider='garmin'", (profile_id,))
    if not conn:
        raise LookupError("Garmin is not linked")
    password = vault.decrypt(conn["password_enc"])
    tokens = vault.decrypt(conn["tokens_enc"])
    st = _personal_state(tokens)
    if st:
        return PersonalApi(password, st.get("token"), float(st.get("exp") or 0))
    g = Garmin(conn["email"], password)
    try:
        g.login(tokenstore=tokens) if tokens else g.login()
    except Exception as e:  # noqa: BLE001
        # Most likely: tokens expired and the fallback password login hit an MFA prompt.
        raise NeedsMfa(str(e)) from e
    return g


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _day(g, profile_id: int, d: str) -> None:
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
    if isinstance(g, PersonalApi):
        g.refresh_token()  # surface a rejected key now; the per-call handlers below would swallow it

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


def _ts(v) -> int | None:
    """Garmin timestamps: epoch milliseconds, or a GMT 'yyyy-MM-ddTHH:mm:ss.S' string."""
    if isinstance(v, (int, float)) and v > 0:
        return int(v // 1000)
    if isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "").replace(" ", "T"))
        except ValueError:
            return None
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
    return None


def parse_weigh_in(w: dict) -> tuple[int, dict] | None:
    """One Garmin weigh-in -> (unix seconds, body values).

    Garmin Connect's own endpoints send weight in grams; the Personal API docs say
    kilograms. Anything over 1000 is grams (nobody weighs a tonne).

    Only fields with the same meaning as the Renpho ones are kept (muscle and bone
    arrive as masses, while the app stores Renpho's percentages, so they're skipped).
    """
    wt = _num(w.get("weight"))
    ts = _ts(w.get("timestampGMT")) or _ts(w.get("date"))
    if not wt or not ts:
        return None
    vals = {
        "weight_kg": round(wt / 1000 if wt > 1000 else wt, 2),
        "bmi": _num(w.get("bmi")),
        "body_fat": _num(w.get("bodyFat")),
        "water": _num(w.get("bodyWater")),
        "visceral_fat": _num(w.get("visceralFat")),
        "metabolic_age": _num(w.get("metabolicAge")),
    }
    return ts, {k: v for k, v in vals.items() if v is not None}


def _weigh_ins(g, profile_id: int, today: date) -> int:
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
