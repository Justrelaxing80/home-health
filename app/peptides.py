"""Peptide tracker: compounds/vials, dosing schedules, dose log, and push reminders.

Times are household wall-clock strings in the container's TZ:
dates 'YYYY-MM-DD', moments 'YYYY-MM-DDTHH:MM'.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import date, datetime, timedelta

from . import db

log = logging.getLogger("home-health.peptides")

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ["TZ"]) if os.environ.get("TZ") else None  # None: use system local time
except Exception:  # noqa: BLE001 - unknown TZ name: fall back to container local time
    TZ = None

SITES = ["Abdomen L", "Abdomen R", "Thigh L", "Thigh R", "Deltoid L", "Deltoid R", "Glute L", "Glute R"]
REMIND_WINDOW_MIN = 20   # send a reminder if we're within this many minutes after the dose time


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


# ---------------- math ----------------

def concentration(p: dict) -> float | None:
    """mcg per mL after reconstitution."""
    if p.get("vial_mg") and p.get("water_ml"):
        return p["vial_mg"] * 1000 / p["water_ml"]
    return None


def units_for(p: dict, dose_mcg: float | None) -> float | None:
    """Insulin-syringe units (U-100: 100 units = 1 mL) to draw for a dose."""
    c = concentration(p)
    if not c or not dose_mcg:
        return None
    return round(dose_mcg / c * 100, 1)


# ---------------- schedule ----------------

def _times(p: dict) -> list[str]:
    out = []
    for t in (p.get("times") or "").split(","):
        t = t.strip()
        if len(t) == 5 and t[2] == ":":
            out.append(t)
    return sorted(set(out))


def scheduled_on(p: dict, d: date) -> bool:
    if not p["active"] or p["freq"] == "as_needed":
        return False
    start = date.fromisoformat(p["start_date"])
    if d < start or (p.get("end_date") and d > date.fromisoformat(p["end_date"])):
        return False
    if p["freq"] == "weekdays":
        return str(d.weekday()) in (p.get("weekdays") or "").split(",")
    if p["freq"] == "interval":
        return (d - start).days % max(1, p.get("interval_days") or 1) == 0
    return True  # daily


def occurrences(peps: list[dict], d: date) -> list[dict]:
    out = []
    for p in peps:
        if scheduled_on(p, d):
            for t in _times(p):
                out.append({"peptide_id": p["id"], "scheduled_for": f"{d.isoformat()}T{t}"})
    return out


def next_dose(p: dict, after: datetime) -> str | None:
    for i in range(0, 120):
        d = after.date() + timedelta(days=i)
        if scheduled_on(p, d):
            for t in _times(p):
                s = f"{d.isoformat()}T{t}"
                if s > after.strftime("%Y-%m-%dT%H:%M"):
                    return s
    return None


# ---------------- queries ----------------

def list_peptides(profile_id: int) -> list[dict]:
    peps = db.rows("SELECT * FROM peptides WHERE profile_id=? ORDER BY active DESC, name", (profile_id,))
    now = now_local()
    for p in peps:
        p["concentration"] = concentration(p)
        p["dose_units"] = units_for(p, p["dose_mcg"])
        used = None
        if p.get("reconstituted_on"):
            r = db.row("SELECT COALESCE(SUM(dose_mcg),0) AS used FROM peptide_logs "
                       "WHERE peptide_id=? AND status='taken' AND taken_at>=?",
                       (p["id"], p["reconstituted_on"]))
            used = r["used"]
            exp = date.fromisoformat(p["reconstituted_on"]) + timedelta(days=p["expiry_days"])
            p["expires_on"] = exp.isoformat()
        else:
            p["expires_on"] = None
        p["used_mcg"] = used
        p["remaining_mcg"] = max(0.0, p["vial_mg"] * 1000 - used) if p.get("vial_mg") and used is not None else None
        p["doses_left"] = (int(p["remaining_mcg"] // p["dose_mcg"])
                           if p["remaining_mcg"] is not None and p["dose_mcg"] else None)
        p["next_dose"] = next_dose(p, now)
    return peps


def get(profile_id: int, peptide_id: int) -> dict | None:
    return db.row("SELECT * FROM peptides WHERE id=? AND profile_id=?", (peptide_id, profile_id))


def day_view(profile_id: int, d: date) -> dict:
    peps = {p["id"]: p for p in db.rows("SELECT * FROM peptides WHERE profile_id=?", (profile_id,))}
    occ = occurrences(list(peps.values()), d)
    logs = db.rows("SELECT * FROM peptide_logs WHERE profile_id=? AND substr(taken_at,1,10)=? "
                   "OR (profile_id=? AND substr(scheduled_for,1,10)=?) ORDER BY taken_at",
                   (profile_id, d.isoformat(), profile_id, d.isoformat()))
    by_slot = {(lg["peptide_id"], lg["scheduled_for"]): lg for lg in logs if lg["scheduled_for"]}
    doses = []
    for o in occ:
        p = peps[o["peptide_id"]]
        lg = by_slot.get((o["peptide_id"], o["scheduled_for"]))
        doses.append({
            **o, "name": p["name"], "color": p["color"], "route": p["route"],
            "dose_mcg": p["dose_mcg"], "dose_unit": p["dose_unit"], "units": units_for(p, p["dose_mcg"]),
            "status": lg["status"] if lg else "pending", "log": lg,
        })
    doses.sort(key=lambda x: (x["scheduled_for"], x["name"]))
    extra = [lg for lg in logs if not lg["scheduled_for"]]
    return {"date": d.isoformat(), "doses": doses, "unscheduled": extra}


def history(profile_id: int, days: int = 30) -> dict:
    now = now_local()
    since = (now.date() - timedelta(days=days - 1)).isoformat()
    logs = db.rows("SELECT * FROM peptide_logs WHERE profile_id=? AND taken_at>=? ORDER BY taken_at DESC",
                   (profile_id, since))
    peps = db.rows("SELECT * FROM peptides WHERE profile_id=?", (profile_id,))
    # Adherence: scheduled doses whose time has passed vs. those logged as taken.
    due = 0
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    for i in range(days):
        d = now.date() - timedelta(days=i)
        due += sum(1 for o in occurrences(peps, d) if o["scheduled_for"] <= stamp)
    taken_sched = db.row(
        "SELECT COUNT(*) AS n FROM peptide_logs WHERE profile_id=? AND status='taken' "
        "AND scheduled_for IS NOT NULL AND scheduled_for>=? AND scheduled_for<=?",
        (profile_id, since, stamp))["n"]
    # Site rotation: when each site was last used (all time).
    last = {r["site"]: r["last"] for r in db.rows(
        "SELECT site, MAX(taken_at) AS last FROM peptide_logs WHERE profile_id=? AND status='taken' "
        "AND site IS NOT NULL AND site<>'' GROUP BY site", (profile_id,))}
    sites = [{"site": s, "last": last.get(s)} for s in SITES]
    suggested = min(sites, key=lambda s: s["last"] or "")["site"]
    return {"logs": logs, "due": due, "taken": taken_sched,
            "adherence": round(100 * min(taken_sched, due) / due) if due else None,
            "sites": sites, "suggested_site": suggested, "days": days}


def summary(profile_id: int) -> dict | None:
    """Small block for the health dashboard."""
    if not db.row("SELECT 1 FROM peptides WHERE profile_id=? AND active=1", (profile_id,)):
        return None
    now = now_local()
    view = day_view(profile_id, now.date())
    taken = sum(1 for d in view["doses"] if d["status"] == "taken")
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    upcoming = [d for d in view["doses"] if d["status"] == "pending" and d["scheduled_for"] >= stamp]
    overdue = [d for d in view["doses"] if d["status"] == "pending" and d["scheduled_for"] < stamp]
    nxt = upcoming[0] if upcoming else None
    if not nxt:
        cands = [(p["next_dose"], p["name"]) for p in list_peptides(profile_id) if p["next_dose"]]
        nxt = {"scheduled_for": min(cands)[0], "name": min(cands)[1]} if cands else None
    return {"scheduled": len(view["doses"]), "taken": taken, "overdue": len(overdue),
            "next": {"scheduled_for": nxt["scheduled_for"], "name": nxt["name"]} if nxt else None}


# ---------------- reminders (ntfy) ----------------

def send_ntfy(server: str, topic: str, title: str, message: str, tags: str = "syringe") -> None:
    # JSON publishing (POST to the server root) keeps UTF-8 intact; header-based
    # publishing would mangle non-ASCII titles.
    body = json.dumps({"topic": topic, "title": title, "message": message,
                       "tags": [tags], "priority": 4}).encode("utf-8")
    req = urllib.request.Request(server.rstrip("/") + "/", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - user-configured URL
        r.read()


def _clock(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _fmt_dose(p: dict) -> str:
    if p["dose_unit"] == "mg":
        s = f"{p['dose_mcg'] / 1000:g} mg"
    else:
        s = f"{p['dose_mcg']:g} mcg"
    u = units_for(p, p["dose_mcg"])
    return f"{s} ({u:g} units)" if u else s


def check_reminders() -> None:
    """Run every minute: push a reminder for each dose that just came due and isn't logged."""
    now = now_local()
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    earliest = (now - timedelta(minutes=REMIND_WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M")
    for cfg in db.rows("SELECT * FROM notify"):
        peps = db.rows("SELECT * FROM peptides WHERE profile_id=? AND active=1 AND reminders=1",
                       (cfg["profile_id"],))
        by_id = {p["id"]: p for p in peps}
        for o in occurrences(peps, now.date()):
            if not (earliest <= o["scheduled_for"] <= stamp):
                continue
            if db.row("SELECT 1 FROM peptide_logs WHERE peptide_id=? AND scheduled_for=?",
                      (o["peptide_id"], o["scheduled_for"])):
                continue
            with db.connect() as c:
                cur = c.execute("INSERT OR IGNORE INTO peptide_reminders_sent VALUES (?, ?)",
                                (o["peptide_id"], o["scheduled_for"]))
                if cur.rowcount == 0:
                    continue  # already sent
            p = by_id[o["peptide_id"]]
            prof = db.row("SELECT name FROM profiles WHERE id=?", (cfg["profile_id"],))
            title = f"Dose reminder · {prof['name']}" if prof else "Dose reminder"
            msg = ("A scheduled dose is due now." if cfg["generic_text"]
                   else f"{p['name']} · {_fmt_dose(p)} · {p['route']} · due {_clock(o['scheduled_for'][11:])}")
            try:
                send_ntfy(cfg["ntfy_server"], cfg["ntfy_topic"], title, msg)
            except Exception as e:  # noqa: BLE001
                log.warning("ntfy reminder failed profile=%s: %s", cfg["profile_id"], e)
    # Housekeeping: forget sent-markers older than a week.
    db.execute("DELETE FROM peptide_reminders_sent WHERE scheduled_for < ?",
               ((now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M"),))
