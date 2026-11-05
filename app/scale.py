"""Renpho smart-scale data: cloud sync (unofficial `renpho-api`) and CSV import."""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timezone

from renpho import RenphoClient

from . import db, vault

log = logging.getLogger("home-health.renpho")

LB_PER_KG = 2.2046226218

# renpho-api field -> our column
CLOUD_MAP = {
    "weight": "weight_kg", "bmi": "bmi", "bodyfat": "body_fat", "water": "water",
    "muscle": "muscle", "bone": "bone", "bmr": "bmr", "visfat": "visceral_fat",
    "subfat": "subcutaneous_fat", "protein": "protein", "bodyage": "metabolic_age",
}


def _float(v):
    try:
        f = float(str(v).replace(",", "").strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


# ---------------- cloud ----------------

def link(profile_id: int, email: str, password: str) -> None:
    client = RenphoClient(email, password)
    client.login()  # raises RenphoAPIError on bad credentials
    with db.connect() as c:
        c.execute(
            "INSERT INTO connections (profile_id, provider, email, password_enc, status, last_error) "
            "VALUES (?, 'renpho', ?, ?, 'linked', NULL) "
            "ON CONFLICT(profile_id, provider) DO UPDATE SET email=excluded.email, "
            "password_enc=excluded.password_enc, status='linked', last_error=NULL",
            (profile_id, email, vault.encrypt(password)),
        )


def sync(profile_id: int) -> dict:
    conn = db.row("SELECT * FROM connections WHERE profile_id=? AND provider='renpho'", (profile_id,))
    if not conn:
        raise LookupError("Renpho is not linked")
    client = RenphoClient(conn["email"], vault.decrypt(conn["password_enc"]) or "")
    client.login()
    added = 0
    for m in client.get_all_measurements():
        ts = _float(m.get("timeStamp") or m.get("time_stamp") or m.get("createdAt"))
        if not ts:
            continue
        if ts > 1e12:  # milliseconds
            ts /= 1000
        values = {col: _float(m.get(key)) for key, col in CLOUD_MAP.items()}
        if db.upsert_body(profile_id, int(ts), "renpho", values):
            added += 1
    with db.connect() as c:
        c.execute(
            "UPDATE connections SET last_sync=? WHERE profile_id=? AND provider='renpho'",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), profile_id),
        )
    return {"measurements": added}


# ---------------- CSV ----------------
# The Renpho app's export headers vary by app version/locale, e.g.
# "Time of Measurement", "Weight(lb)", "Body Fat(%)", "Visceral Fat",
# "Body Water(%)", "Skeletal Muscle(%)", "Bone Mass(lb)", "BMR(kcal)", ...
# so columns are matched by keyword rather than exact name.

CSV_COLUMNS = [
    # (our column, keywords that must all appear, keywords that must not)
    ("weight_kg", ("weight",), ("fat-free", "fat free", "lean", "muscle")),
    ("bmi", ("bmi",), ()),
    ("body_fat", ("body fat",), ("subcutaneous", "visceral", "free", "mass")),
    ("subcutaneous_fat", ("subcutaneous",), ()),
    ("visceral_fat", ("visceral",), ()),
    ("water", ("water",), ()),
    ("muscle", ("muscle",), ("mass",)),
    ("bone", ("bone",), ()),
    ("bmr", ("bmr",), ()),
    ("protein", ("protein",), ()),
    ("metabolic_age", ("age",), ()),
]

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p",
    "%d/%m/%Y %H:%M", "%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y",
)


def _parse_when(s: str) -> int | None:
    s = re.sub(r"\s+", " ", (s or "").strip())
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            return int(datetime.strptime(s, fmt).timestamp())  # local time of the server
        except ValueError:
            continue
    return None


def _match(header: str, must: tuple, must_not: tuple) -> bool:
    h = header.lower()
    return all(k in h for k in must) and not any(k in h for k in must_not)


def import_csv(profile_id: int, data: bytes) -> dict:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("The file is empty.")

    date_idx = next((i for i, h in enumerate(header)
                     if any(k in h.lower() for k in ("time", "date"))), None)
    if date_idx is None:
        raise ValueError("Couldn't find a date/time column. Is this a Renpho export?")

    cols: dict[str, int] = {}
    units: dict[str, str] = {}
    for i, h in enumerate(header):
        for col, must, must_not in CSV_COLUMNS:
            if col not in cols and _match(h, must, must_not):
                cols[col] = i
                hl = h.lower()
                units[col] = "lb" if ("lb" in hl or "lbs" in hl) else ("st" if "(st" in hl else "kg")
                break
    if "weight_kg" not in cols:
        raise ValueError("Couldn't find a weight column. Is this a Renpho export?")

    added = skipped = 0
    for rec in reader:
        if not rec or len(rec) <= date_idx:
            continue
        ts = _parse_when(rec[date_idx])
        if ts is None:
            skipped += 1
            continue
        values = {}
        for col, i in cols.items():
            if i < len(rec):
                v = _float(re.sub(r"[^\d.,\-]", "", rec[i]) if col == "weight_kg" else rec[i])
                if v is not None and col in ("weight_kg", "bone") and units[col] == "lb":
                    v = v / LB_PER_KG
                values[col] = v
        if db.upsert_body(profile_id, ts, "renpho-csv", values):
            added += 1
        else:
            skipped += 1
    return {"imported": added, "skipped": skipped, "columns": sorted(cols)}
