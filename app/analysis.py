"""Daily summary paragraph for the dashboard.

Deterministic and data-only: every number below is either a stored reading or a
plain average of stored readings, and each one names the day it came from.
Metrics with no recent data are left out rather than guessed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .peptides import TZ, now_local

LB_PER_KG = 2.2046226218
STALE_DAYS = 3        # daily metrics older than this are left out
WEIGHT_STALE_DAYS = 14


def _day(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today - timedelta(days=1):
        return "yesterday"
    return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"


def _on(d: date, today: date) -> str:
    s = _day(d, today)
    return s if s in ("today", "yesterday") else f"on {s}"


def _hm(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}h {round((sec % 3600) / 60):02d}m"


def _n(v: float) -> str:
    return f"{round(v):,}"


def _avg(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _latest(daily: list[dict], key: str, today: date):
    """(row, baseline rows before it) for the most recent non-stale value."""
    have = [r for r in daily if r.get(key) is not None]
    if not have:
        return None, []
    last = have[-1]
    if (today - date.fromisoformat(last["date"])).days > STALE_DAYS:
        return None, []
    return last, have[:-1]


def _compare(diff: float, small: float, unit_word: str, more: str, less: str) -> str:
    if abs(diff) < small:
        return "about the same as"
    return f"{unit_word.format(abs(diff))} {more if diff > 0 else less}"


def build(profile: dict, daily: list[dict], body: list[dict], activities: list[dict],
          supp: dict | None, pep: dict | None) -> dict | None:
    today = now_local().date()
    parts: list[str] = []

    # ---- sleep ----
    row, base = _latest(daily, "sleep_s", today)
    if row:
        d = date.fromisoformat(row["date"])
        lead = "Last night you slept" if d == today else f"For the night ending {_day(d, today)} you slept"
        s = f"{lead} {_hm(row['sleep_s'])}"
        if row.get("sleep_score") is not None:
            s += f" (sleep score {row['sleep_score']})"
        prev = [r["sleep_s"] for r in base[-7:]]
        if len(prev) >= 3:
            a = _avg(prev)
            diff = row["sleep_s"] - a
            if abs(diff) < 15 * 60:
                s += f", about the same as your {len(prev)}-night average of {_hm(a)}"
            else:
                s += (f", {round(abs(diff) / 60)} min {'more' if diff > 0 else 'less'} than your "
                      f"{len(prev)}-night average of {_hm(a)}")
        parts.append(s + ".")

    # ---- resting HR + HRV ----
    hr_bits = []
    row, base = _latest(daily, "resting_hr", today)
    if row:
        s = f"Resting heart rate was {row['resting_hr']} bpm {_on(date.fromisoformat(row['date']), today)}"
        prev = [r["resting_hr"] for r in base[-30:]]
        if len(prev) >= 5:
            a = _avg(prev)
            diff = row["resting_hr"] - a
            s += (f", in line with your {len(prev)}-day average ({_n(a)} bpm)" if abs(diff) < 2 else
                  f", {_n(abs(diff))} bpm {'above' if diff > 0 else 'below'} your {len(prev)}-day average ({_n(a)} bpm)")
        hr_bits.append(s)
    row, base = _latest(daily, "hrv_avg", today)
    if row:
        s = f"HRV was {row['hrv_avg']} ms"
        if not hr_bits:
            s += f" {_on(date.fromisoformat(row['date']), today)}"
        prev = [r["hrv_avg"] for r in base[-30:]]
        if len(prev) >= 5:
            a = _avg(prev)
            pct = (row["hrv_avg"] - a) / a * 100 if a else 0
            s += (f", in line with your {len(prev)}-day average ({_n(a)} ms)" if abs(pct) < 5 else
                  f", {_n(abs(pct))}% {'above' if pct > 0 else 'below'} your {len(prev)}-day average ({_n(a)} ms)")
        hr_bits.append(s)
    if hr_bits:
        parts.append("; ".join(hr_bits) + ".")

    # ---- steps (prefer yesterday's complete total) ----
    steps = {r["date"]: r["steps"] for r in daily if r.get("steps") is not None}
    y = (today - timedelta(days=1)).isoformat()
    goal = profile.get("step_goal") or 10000
    if y in steps:
        s = f"Yesterday you took {_n(steps[y])} steps, {_n(steps[y] / goal * 100)}% of your {_n(goal)} goal"
        if today.isoformat() in steps:
            s += f", and {_n(steps[today.isoformat()])} so far today"
        parts.append(s + ".")
    else:
        row, _ = _latest(daily, "steps", today)
        if row:
            d = date.fromisoformat(row["date"])
            when = "so far today" if d == today else f"on {_day(d, today)}"
            parts.append(f"You took {_n(row['steps'])} steps {when}.")

    # ---- Body Battery / stress (Garmin) ----
    row, _ = _latest(daily, "body_battery_high", today)
    if row and row.get("body_battery_low") is not None:
        s = (f"Body Battery ranged {row['body_battery_low']}–{row['body_battery_high']} "
             f"{_on(date.fromisoformat(row['date']), today)}")
        if row.get("avg_stress") is not None:
            s += f" with an average stress level of {row['avg_stress']}"
        parts.append(s + ".")

    # ---- weight / body fat ----
    if body:
        last = body[-1]
        last_d = datetime.fromtimestamp(last["measured_at"], TZ).date()
        if last.get("weight_kg") and (today - last_d).days <= WEIGHT_STALE_DAYS:
            metric = profile.get("units") == "metric"
            w = lambda kg: kg if metric else kg * LB_PER_KG  # noqa: E731
            u = "kg" if metric else "lb"
            s = f"Latest weigh-in ({_day(last_d, today)}): {w(last['weight_kg']):.1f} {u}"
            first = next((b for b in body if b.get("weight_kg")), None)
            first_d = datetime.fromtimestamp(first["measured_at"], TZ).date() if first else None
            if first and (last_d - first_d).days >= 7:
                diff = w(last["weight_kg"]) - w(first["weight_kg"])
                change = "unchanged" if abs(diff) < 0.05 else f"{'up' if diff > 0 else 'down'} {abs(diff):.1f} {u}"
                s += f", {change} since {_day(first_d, today)} ({w(first['weight_kg']):.1f} {u})"
                if last.get("body_fat") is not None and first.get("body_fat") is not None:
                    fd = last["body_fat"] - first["body_fat"]
                    s += (f"; body fat {last['body_fat']:.1f}% "
                          f"({'+' if fd > 0 else '−' if fd < 0 else '±'}{abs(fd):.1f} points)")
            elif last.get("body_fat") is not None:
                s += f"; body fat {last['body_fat']:.1f}%"
            parts.append(s + ".")

    # ---- workouts ----
    if activities:  # last 30 days; say nothing if this person doesn't log workouts
        week_ago = (today - timedelta(days=6)).isoformat()
        recent = [a for a in activities if a["start_time"][:10] >= week_ago]
        if recent:
            total = sum(a.get("duration_s") or 0 for a in recent)
            parts.append(f"{len(recent)} workout{'s' if len(recent) != 1 else ''} logged in the last 7 days"
                         f"{f' ({_hm(total)} total)' if total else ''}.")
        else:
            parts.append("No workouts logged in the last 7 days.")

    # ---- supplements / peptides ----
    if supp:
        st = supp["streak"]
        s = (f"Supplements: {supp['taken']} of {supp['due']} servings taken today"
             if supp["due"] else "Supplements: none scheduled today")
        s += f"; {st['current']}-day streak" + (f" (best {st['best']})" if st["best"] > st["current"] else "")
        parts.append(s + ".")
    if pep:
        st = pep.get("streak") or {"current": 0, "best": 0}
        s = (f"Peptides: {pep['taken']} of {pep['scheduled']} doses logged today"
             if pep["scheduled"] else "Peptides: no doses scheduled today")
        if pep.get("overdue"):
            s += f" ({pep['overdue']} overdue)"
        s += f"; {st['current']}-day streak" + (f" (best {st['best']})" if st["best"] > st["current"] else "")
        parts.append(s + ".")

    if not parts:
        return None
    return {"date": today.isoformat(), "text": " ".join(parts), "sentences": parts}
