"""Supplements: daily checklist, per-day servings, and streaks."""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

from . import db, streak
from .peptides import REMIND_WINDOW_MIN, now_local, send_ntfy

log = logging.getLogger("home-health.supplements")

# The 25 most common supplements, with a typical label serving as the starting
# amount. These are defaults to edit to match your product, not recommendations.
CATALOG = [
    {"name": "Protein powder", "amount": 25, "unit": "g", "time_label": "After workout"},
    {"name": "Creatine monohydrate", "amount": 5, "unit": "g", "time_label": "Morning"},
    {"name": "Vitamin D3", "amount": 2000, "unit": "IU", "time_label": "Morning"},
    {"name": "Vitamin B12", "amount": 1000, "unit": "mcg", "time_label": "Morning"},
    {"name": "Fish oil (omega-3)", "amount": 1000, "unit": "mg", "time_label": "With meals"},
    {"name": "Magnesium glycinate", "amount": 200, "unit": "mg", "time_label": "Bedtime"},
    {"name": "Multivitamin", "amount": 1, "unit": "tablet", "time_label": "Morning"},
    {"name": "Vitamin C", "amount": 500, "unit": "mg", "time_label": "Morning"},
    {"name": "Zinc", "amount": 15, "unit": "mg", "time_label": "With meals"},
    {"name": "Probiotic", "amount": 1, "unit": "capsule", "time_label": "Morning"},
    {"name": "Collagen peptides", "amount": 10, "unit": "g", "time_label": "Morning"},
    {"name": "Vitamin K2 (MK-7)", "amount": 100, "unit": "mcg", "time_label": "Morning"},
    {"name": "Ashwagandha", "amount": 600, "unit": "mg", "time_label": "Evening"},
    {"name": "Iron", "amount": 18, "unit": "mg", "time_label": "Morning"},
    {"name": "Calcium", "amount": 500, "unit": "mg", "time_label": "With meals"},
    {"name": "Turmeric / curcumin", "amount": 500, "unit": "mg", "time_label": "With meals"},
    {"name": "Melatonin", "amount": 3, "unit": "mg", "time_label": "Bedtime"},
    {"name": "Electrolytes", "amount": 1, "unit": "serving", "time_label": "Morning"},
    {"name": "Vitamin B complex", "amount": 1, "unit": "capsule", "time_label": "Morning"},
    {"name": "Folate", "amount": 400, "unit": "mcg", "time_label": "Morning"},
    {"name": "CoQ10", "amount": 100, "unit": "mg", "time_label": "With meals"},
    {"name": "Psyllium fiber", "amount": 5, "unit": "g", "time_label": "Morning"},
    {"name": "L-theanine", "amount": 200, "unit": "mg", "time_label": "Evening"},
    {"name": "Glucosamine", "amount": 1500, "unit": "mg", "time_label": "With meals"},
    {"name": "NAC (N-acetylcysteine)", "amount": 600, "unit": "mg", "time_label": "Morning"},
]
UNITS = ["g", "mg", "mcg", "IU", "mL", "capsule", "tablet", "softgel", "scoop", "serving", "drop"]
TIME_LABELS = ["", "Morning", "Afternoon", "Evening", "Bedtime", "With meals", "Before workout", "After workout"]


def today() -> date:
    return now_local().date()


def scheduled_on(s: dict, d: date) -> bool:
    if not s["active"] or d < date.fromisoformat(s["start_date"]):
        return False
    if s["freq"] == "weekdays":
        return str(d.weekday()) in (s.get("weekdays") or "").split(",")
    return True


def get(profile_id: int, sid: int) -> dict | None:
    return db.row("SELECT * FROM supplements WHERE id=? AND profile_id=?", (sid, profile_id))


def _counts(profile_id: int, since: str) -> dict[tuple[int, str], int]:
    return {(r["supplement_id"], r["date"]): r["n"] for r in db.rows(
        "SELECT supplement_id, date, COUNT(*) AS n FROM supplement_logs "
        "WHERE profile_id=? AND date>=? GROUP BY supplement_id, date", (profile_id, since))}


def _streaks(sups: list[dict], counts: dict, t: date) -> tuple[dict, dict]:
    """Overall streak (every scheduled supplement fully taken) and per-supplement streaks."""
    active = [s for s in sups if s["active"]]
    earliest = min((date.fromisoformat(s["start_date"]) for s in active), default=None)

    def day_status(group):
        def f(d: date):
            due = [s for s in group if scheduled_on(s, d)]
            if not due:
                return None
            ok = all(counts.get((s["id"], d.isoformat()), 0) >= s["servings"] for s in due)
            return "done" if ok else "missed"
        return f

    overall = streak.compute(day_status(active), t, earliest)
    each = {s["id"]: streak.compute(day_status([s]), t, date.fromisoformat(s["start_date"]))
            for s in active}
    return overall, each


def page(profile_id: int, d: date) -> dict:
    t = today()
    sups = db.rows("SELECT * FROM supplements WHERE profile_id=? ORDER BY active DESC, "
                   "CASE time_label WHEN 'Morning' THEN 1 WHEN 'With meals' THEN 2 WHEN 'Afternoon' THEN 3 "
                   "WHEN 'Before workout' THEN 4 WHEN 'After workout' THEN 5 WHEN 'Evening' THEN 6 "
                   "WHEN 'Bedtime' THEN 7 ELSE 8 END, name", (profile_id,))
    since = (t - timedelta(days=730)).isoformat()
    counts = _counts(profile_id, since)
    overall, each = _streaks(sups, counts, t)
    grid_days = [(t - timedelta(days=i)) for i in range(13, -1, -1)]
    for s in sups:
        s["scheduled"] = scheduled_on(s, d)
        s["taken"] = counts.get((s["id"], d.isoformat()), 0)
        s["streak"] = each.get(s["id"], {"current": 0, "best": 0})
        s["grid"] = [
            None if not scheduled_on(s, g) else
            ("done" if counts.get((s["id"], g.isoformat()), 0) >= s["servings"] else
             "partial" if counts.get((s["id"], g.isoformat()), 0) else
             "today" if g == t else "missed")
            for g in grid_days]
    due = [s for s in sups if s["scheduled"]]
    return {"day": d.isoformat(), "today": t.isoformat(), "supplements": sups,
            "grid_days": [g.isoformat() for g in grid_days],
            "servings_due": sum(s["servings"] for s in due),
            "servings_taken": sum(min(s["taken"], s["servings"]) for s in due),
            "streak": overall, "catalog": CATALOG, "units": UNITS, "time_labels": TIME_LABELS}


def summary(profile_id: int) -> dict | None:
    """Small block for the health dashboard."""
    if not db.row("SELECT 1 FROM supplements WHERE profile_id=? AND active=1", (profile_id,)):
        return None
    p = page(profile_id, today())
    return {"due": p["servings_due"], "taken": p["servings_taken"], "streak": p["streak"],
            "count": sum(1 for s in p["supplements"] if s["scheduled"])}


# ---------------- reminders (ntfy) ----------------

def _fmt(s: dict) -> str:
    if s.get("amount") is None:
        return s["name"]
    return f"{s['name']} ({s['amount']:,g}{' ' + s['unit'] if s['unit'] else ''})"


def due_reminders(sups: list[dict], counts: dict, d: date, earliest: str, stamp: str) -> list[tuple[str, dict]]:
    """(slot, supplement) pairs whose reminder time falls in [earliest, stamp] and that still
    need a serving. With n reminder times and k servings, the i-th reminder expects
    ceil(k*i/n) servings checked off by then (one time = remind until all are taken)."""
    out = []
    for s in sups:
        if not scheduled_on(s, d):
            continue
        times = sorted({t for t in (s.get("remind_times") or "").split(",") if t})
        for i, t in enumerate(times, 1):
            slot = f"{d.isoformat()}T{t}"
            if not (earliest <= slot <= stamp):
                continue
            expected = math.ceil(s["servings"] * i / len(times))
            if counts.get(s["id"], 0) < expected:
                out.append((slot, s))
    return out


def check_reminders() -> None:
    """Run every minute: one notification per profile and time listing the supplements still due."""
    now = now_local()
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    earliest = (now - timedelta(minutes=REMIND_WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M")
    d = now.date()
    for cfg in db.rows("SELECT * FROM notify"):
        pid = cfg["profile_id"]
        sups = db.rows("SELECT * FROM supplements WHERE profile_id=? AND active=1 AND remind_times<>''", (pid,))
        if not sups:
            continue
        counts = {r["supplement_id"]: r["n"] for r in db.rows(
            "SELECT supplement_id, COUNT(*) AS n FROM supplement_logs WHERE profile_id=? AND date=? "
            "GROUP BY supplement_id", (pid, d.isoformat()))}
        groups: dict[str, list[dict]] = {}
        for slot, s in due_reminders(sups, counts, d, earliest, stamp):
            with db.connect() as c:
                cur = c.execute("INSERT OR IGNORE INTO supplement_reminders_sent VALUES (?, ?)", (s["id"], slot))
                if cur.rowcount == 0:
                    continue  # already sent
            groups.setdefault(slot, []).append(s)
        prof = db.row("SELECT name FROM profiles WHERE id=?", (pid,))
        for slot, items in sorted(groups.items()):
            title = f"Supplements · {prof['name']}" if prof else "Supplements"
            msg = ("Time to take your supplements." if cfg["generic_text"]
                   else "Time for: " + ", ".join(_fmt(s) for s in items))
            try:
                send_ntfy(cfg["ntfy_server"], cfg["ntfy_topic"], title, msg, tags="pill")
            except Exception as e:  # noqa: BLE001
                log.warning("ntfy supplement reminder failed profile=%s: %s", pid, e)
    db.execute("DELETE FROM supplement_reminders_sent WHERE slot < ?",
               ((now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M"),))
