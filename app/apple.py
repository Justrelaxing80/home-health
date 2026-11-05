"""Apple Health: there is no server-side API, so data arrives from the iPhone.

Two paths:
  * Push: the iOS app "Health Auto Export" POSTs JSON to /api/ingest/apple/<token>
    on a schedule (its REST API automation).
  * Upload: the Health app's "Export All Health Data" export.zip, parsed by streaming.

Daily values go to `apple_daily`; weigh-ins to `body` (source 'apple');
workouts to `activities` (ids prefixed 'apple-').
"""

from __future__ import annotations

import hmac
import logging
import secrets
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import datetime

from . import db, vault

log = logging.getLogger("home-health.apple")

LB_PER_KG = 2.2046226218


# ---------------- token / connection ----------------

def enable(profile_id: int) -> str:
    """Create (or rotate) the ingest token for a profile. Returns the token."""
    token = secrets.token_urlsafe(24)
    with db.connect() as c:
        c.execute(
            "INSERT INTO connections (profile_id, provider, email, tokens_enc, status, last_error) "
            "VALUES (?, 'apple', '', ?, 'linked', NULL) "
            "ON CONFLICT(profile_id, provider) DO UPDATE SET tokens_enc=excluded.tokens_enc, "
            "status='linked', last_error=NULL",
            (profile_id, vault.encrypt(token)),
        )
    return token


def token_for(profile_id: int) -> str | None:
    conn = db.row("SELECT tokens_enc FROM connections WHERE profile_id=? AND provider='apple'", (profile_id,))
    return vault.decrypt(conn["tokens_enc"]) if conn and conn["tokens_enc"] else None


def profile_for_token(token: str) -> int | None:
    for c in db.rows("SELECT profile_id, tokens_enc FROM connections WHERE provider='apple'"):
        t = vault.decrypt(c["tokens_enc"]) if c["tokens_enc"] else None
        if t and hmac.compare_digest(t, token):
            return c["profile_id"]
    return None


def _ensure_connection(profile_id: int) -> None:
    if not db.row("SELECT 1 FROM connections WHERE profile_id=? AND provider='apple'", (profile_id,)):
        enable(profile_id)


def _mark(profile_id: int, status: str, error: str | None = None, synced: bool = False) -> None:
    sql = "UPDATE connections SET status=?, last_error=?" + (", last_sync=datetime('now')" if synced else "")
    db.execute(sql + " WHERE profile_id=? AND provider='apple'", (status, error, profile_id))


# ---------------- helpers ----------------

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _day(s: str) -> str | None:
    s = (s or "").strip()
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else None


def _kcal(qty, units: str | None):
    q = _num(qty)
    if q is None:
        return None
    return q / 4.184 if (units or "").lower() in ("kj", "kilojoule", "kilojoules") else q


def _kg(qty, units: str | None):
    q = _num(qty)
    if q is None:
        return None
    u = (units or "").lower()
    if u in ("lb", "lbs", "pound", "pounds"):
        return q / LB_PER_KG
    if u in ("g", "gram", "grams"):
        return q / 1000
    if u in ("st", "stone"):
        return q * 6.35029318
    return q


def _meters(qty, units: str | None):
    q = _num(qty)
    if q is None:
        return None
    u = (units or "").lower()
    return {"mi": q * 1609.344, "km": q * 1000, "yd": q * 0.9144, "ft": q * 0.3048}.get(u, q)


def _pct(v):
    v = _num(v)
    if v is None:
        return None
    return v * 100 if v <= 1 else v


class _Collector:
    """Accumulates per-day values from either source, then writes them once."""

    def __init__(self):
        self.sums = defaultdict(lambda: defaultdict(float))     # (metric, day) -> source -> sum
        self.avgs = defaultdict(list)                            # (metric, day) -> [values]
        self.sleep = defaultdict(lambda: defaultdict(float))    # (day, source) -> stage -> seconds
        self.body = {}                                           # day -> {ts, weight_kg, ...}
        self.body_extra = defaultdict(dict)                      # day -> {body_fat, bmi}
        self.workouts = []

    def add_sum(self, metric, day, value, source=""):
        if day and value is not None:
            self.sums[(metric, day)][source] += value

    def add_avg(self, metric, day, value):
        if day and value is not None:
            self.avgs[(metric, day)].append(value)

    def add_weight(self, dt: datetime, kg):
        if dt is None or not kg:
            return
        day = dt.date().isoformat()
        ts = int(dt.timestamp())
        cur = self.body.get(day)
        if not cur or ts > cur["ts"]:
            self.body[day] = {"ts": ts, "weight_kg": round(kg, 2)}

    def write(self, profile_id: int) -> dict:
        days = defaultdict(dict)
        # Cumulative metrics: the iPhone and the Watch both record steps etc., so
        # summing across sources double-counts. Take the largest single source.
        for (metric, day), by_src in self.sums.items():
            days[day][metric] = max(by_src.values())
        for (metric, day), vals in self.avgs.items():
            days[day][metric] = sum(vals) / len(vals)
        for (day, _src), stages in self._best_sleep().items():
            asleep = stages.get("core", 0) + stages.get("deep", 0) + stages.get("rem", 0) + stages.get("asleep", 0)
            if asleep <= 0:
                continue
            days[day].update({
                "sleep_s": asleep, "deep_s": stages.get("deep") or None,
                "light_s": stages.get("core") or None, "rem_s": stages.get("rem") or None,
                "awake_s": stages.get("awake") or None,
            })
        n_days = 0
        for day, vals in days.items():
            out = {}
            for k, v in vals.items():
                if v is None:
                    continue
                out[k] = round(v, 1) if k == "floors" else int(round(v))
            if "active_kcal" in out and "basal_kcal" in out:
                out["total_kcal"] = out["active_kcal"] + out["basal_kcal"]
            out.pop("basal_kcal", None)
            if out:
                db.upsert_daily(profile_id, day, out, table="apple_daily")
                n_days += 1
        n_body = 0
        for day, b in self.body.items():
            vals = {"weight_kg": b["weight_kg"], **self.body_extra.get(day, {})}
            if db.upsert_body(profile_id, b["ts"], "apple", vals):
                n_body += 1
        for w in self.workouts:
            db.upsert_activity(profile_id, w)
        return {"days": n_days, "weigh_ins": n_body, "workouts": len(self.workouts)}

    def _best_sleep(self):
        """Per night, keep the single source with the most recorded sleep."""
        best = {}
        for (day, src), stages in self.sleep.items():
            total = sum(v for k, v in stages.items() if k != "awake")
            if day not in best or total > best[day][1]:
                best[day] = ((day, src), total, stages)
        return {key: stages for key, _t, stages in best.values()}


# ---------------- Health Auto Export JSON ----------------

# Health Auto Export metric name -> (kind, our column)
HAE_METRICS = {
    "step_count": ("sum", "steps"),
    "flights_climbed": ("sum", "floors"),
    "apple_exercise_time": ("sum", "intensity_min"),
    "active_energy": ("energy", "active_kcal"),
    "basal_energy_burned": ("energy", "basal_kcal"),
    "resting_heart_rate": ("avg", "resting_hr"),
    "heart_rate_variability": ("avg", "hrv_avg"),
}


def ingest_json(profile_id: int, payload: dict) -> dict:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    col = _Collector()
    for m in data.get("metrics") or []:
        name = (m.get("name") or "").lower()
        units = m.get("units")
        points = m.get("data") or []
        if name in HAE_METRICS:
            kind, column = HAE_METRICS[name]
            for p in points:
                day = _day(p.get("date"))
                if kind == "avg":
                    col.add_avg(column, day, _num(p.get("qty", p.get("Avg"))))
                elif kind == "energy":
                    col.add_sum(column, day, _kcal(p.get("qty"), units), p.get("source") or "")
                else:
                    col.add_sum(column, day, _num(p.get("qty")), p.get("source") or "")
        elif name == "sleep_analysis":
            for p in points:
                day = _day(p.get("sleepEnd") or p.get("date"))
                if not day:
                    continue
                factor = 60 if (units or "").lower() in ("min", "minutes") else 3600
                stages = col.sleep[(day, p.get("source") or "")]
                core, deep, rem = (_num(p.get(k)) or 0 for k in ("core", "deep", "rem"))
                asleep = _num(p.get("asleep")) or 0
                if not (core or deep or rem) and not asleep:
                    asleep = _num(p.get("totalSleep")) or 0
                for k, v in (("core", core), ("deep", deep), ("rem", rem), ("asleep", asleep),
                             ("awake", _num(p.get("awake")) or 0)):
                    stages[k] += v * factor
        elif name in ("weight_body_mass", "body_mass"):
            for p in points:
                col.add_weight(_parse_dt(p.get("date")), _kg(p.get("qty"), units))
        elif name == "body_fat_percentage":
            for p in points:
                day = _day(p.get("date"))
                if day:
                    col.body_extra[day]["body_fat"] = _pct(p.get("qty"))
        elif name == "body_mass_index":
            for p in points:
                day = _day(p.get("date"))
                if day:
                    col.body_extra[day]["bmi"] = _num(p.get("qty"))
    for w in data.get("workouts") or []:
        wk = _hae_workout(w)
        if wk:
            col.workouts.append(wk)
    _ensure_connection(profile_id)
    result = col.write(profile_id)
    _mark(profile_id, "linked", None, synced=True)
    log.info("apple ingest profile=%s %s", profile_id, result)
    return result


def _q(obj):
    """Health Auto Export wraps quantities as {"qty": x, "units": u}; accept bare numbers too."""
    if isinstance(obj, dict):
        return obj.get("qty"), obj.get("units")
    return obj, None


def _hae_workout(w: dict) -> dict | None:
    start = _parse_dt(w.get("start"))
    if not start:
        return None
    end = _parse_dt(w.get("end"))
    duration = _num(w.get("duration"))
    if duration is None and end:
        duration = (end - start).total_seconds()
    kcal = _kcal(*_q(w.get("activeEnergyBurned") or w.get("activeEnergy")))
    dist = _meters(*_q(w.get("distance")))
    hr = w.get("avgHeartRate") or (w.get("heartRate") or {}).get("avg")
    name = w.get("name") or "Workout"
    return {
        "activity_id": f"apple-{w.get('id') or start.isoformat()}",
        "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "name": name, "type": name.lower().replace(" ", "_"),
        "duration_s": duration, "distance_m": dist, "calories": kcal,
        "avg_hr": _num(_q(hr)[0]),
    }


# ---------------- export.zip ----------------

HK = "HKQuantityTypeIdentifier"
XML_SUMS = {f"{HK}StepCount": "steps", f"{HK}FlightsClimbed": "floors",
            f"{HK}AppleExerciseTime": "intensity_min"}
XML_ENERGY = {f"{HK}ActiveEnergyBurned": "active_kcal", f"{HK}BasalEnergyBurned": "basal_kcal"}
XML_AVGS = {f"{HK}RestingHeartRate": "resting_hr", f"{HK}HeartRateVariabilitySDNN": "hrv_avg"}
SLEEP_STAGE = {
    "HKCategoryValueSleepAnalysisAsleepCore": "core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "asleep",
    "HKCategoryValueSleepAnalysisAsleep": "asleep",
    "HKCategoryValueSleepAnalysisAwake": "awake",
}


def _find_export_xml(zf: zipfile.ZipFile) -> str:
    for n in zf.namelist():
        base = n.rsplit("/", 1)[-1].lower()
        if base == "export.xml":
            return n
    raise ValueError("This zip has no export.xml — upload the export.zip from the Health app.")


def import_export(profile_id: int, path: str) -> dict:
    """Parse a Health app export (export.zip or a bare export.xml) at `path`."""
    _ensure_connection(profile_id)
    _mark(profile_id, "syncing")
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf, zf.open(_find_export_xml(zf)) as fh:
                result = _parse_xml(profile_id, fh)
        else:
            with open(path, "rb") as fh:
                result = _parse_xml(profile_id, fh)
    except Exception as e:  # noqa: BLE001
        log.exception("apple import failed profile=%s", profile_id)
        _mark(profile_id, "error", f"Health export import failed: {e}"[:500])
        raise
    _mark(profile_id, "linked", None, synced=True)
    log.info("apple export import profile=%s %s", profile_id, result)
    return result


def _parse_xml(profile_id: int, fh) -> dict:
    col = _Collector()
    root = None
    depth = 0
    for event, el in ET.iterparse(fh, events=("start", "end")):
        if event == "start":
            if root is None:
                root = el
            depth += 1
            continue
        depth -= 1
        if depth != 1:
            continue  # only act on direct children of <HealthData>
        if el.tag == "Record":
            _xml_record(col, el.attrib)
        elif el.tag == "Workout":
            wk = _xml_workout(el)
            if wk:
                col.workouts.append(wk)
        root.clear()  # keep memory flat on multi-GB exports
    if root is None or root.tag != "HealthData":
        raise ValueError("Not an Apple Health export.xml")
    return col.write(profile_id)


def _xml_record(col: _Collector, a: dict) -> None:
    t = a.get("type", "")
    if t in XML_SUMS:
        col.add_sum(XML_SUMS[t], _day(a.get("startDate")), _num(a.get("value")), a.get("sourceName", ""))
    elif t in XML_ENERGY:
        col.add_sum(XML_ENERGY[t], _day(a.get("startDate")), _kcal(a.get("value"), a.get("unit")),
                    a.get("sourceName", ""))
    elif t in XML_AVGS:
        col.add_avg(XML_AVGS[t], _day(a.get("startDate")), _num(a.get("value")))
    elif t == "HKCategoryTypeIdentifierSleepAnalysis":
        stage = SLEEP_STAGE.get(a.get("value", ""))
        start, end = _parse_dt(a.get("startDate")), _parse_dt(a.get("endDate"))
        if stage and start and end and end > start:
            col.sleep[(end.date().isoformat(), a.get("sourceName", ""))][stage] += (end - start).total_seconds()
    elif t == f"{HK}BodyMass":
        col.add_weight(_parse_dt(a.get("startDate")), _kg(a.get("value"), a.get("unit")))
    elif t == f"{HK}BodyFatPercentage":
        day = _day(a.get("startDate"))
        if day:
            col.body_extra[day]["body_fat"] = _pct(a.get("value"))
    elif t == f"{HK}BodyMassIndex":
        day = _day(a.get("startDate"))
        if day:
            col.body_extra[day]["bmi"] = _num(a.get("value"))


def _xml_workout(el) -> dict | None:
    a = el.attrib
    start = _parse_dt(a.get("startDate"))
    if not start:
        return None
    dur = _num(a.get("duration"))
    unit = (a.get("durationUnit") or "min").lower()
    if dur is not None:
        dur = dur * 60 if unit.startswith("min") else dur * 3600 if unit.startswith("h") else dur
    dist = _meters(a.get("totalDistance"), a.get("totalDistanceUnit"))
    kcal = _kcal(a.get("totalEnergyBurned"), a.get("totalEnergyBurnedUnit"))
    hr = None
    for st in el.iter("WorkoutStatistics"):  # newer exports put totals here
        typ = st.get("type", "")
        if typ == f"{HK}ActiveEnergyBurned" and kcal is None:
            kcal = _kcal(st.get("sum"), st.get("unit"))
        elif typ.startswith(f"{HK}Distance") and dist is None:
            dist = _meters(st.get("sum"), st.get("unit"))
        elif typ == f"{HK}HeartRate":
            hr = _num(st.get("average"))
    kind = (a.get("workoutActivityType") or "HKWorkoutActivityTypeOther").replace("HKWorkoutActivityType", "")
    pretty = "".join(" " + ch if ch.isupper() and i else ch for i, ch in enumerate(kind)).strip()
    return {
        "activity_id": f"apple-{start.isoformat()}",
        "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "name": pretty, "type": pretty.lower().replace(" ", "_"),
        "duration_s": dur, "distance_m": dist, "calories": kcal, "avg_hr": hr,
    }


def mark_importing(profile_id: int) -> None:
    _ensure_connection(profile_id)
    _mark(profile_id, "syncing")
