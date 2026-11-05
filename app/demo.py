"""Generates a plausible 120-day history so the dashboard can be tried without accounts."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta

from . import db

ACTIVITIES = [("running", "Morning Run", 5200, 1900), ("cycling", "Evening Ride", 18000, 3600),
              ("walking", "Lunch Walk", 2400, 1500), ("strength_training", "Strength", 0, 2700)]


def create_demo_profile(name: str = "Demo") -> int:
    rnd = random.Random(42)
    pid = db.execute("INSERT INTO profiles (name, color, units) VALUES (?, ?, 'imperial')", (name, "#1baf7a"))
    today = date.today()
    weight = 86.0
    fat = 24.5
    for i in range(119, -1, -1):
        d = today - timedelta(days=i)
        weekend = d.weekday() >= 5
        deep = rnd.randint(3600, 6600)
        rem = rnd.randint(4800, 7800)
        light = rnd.randint(11000, 15500)
        awake = rnd.randint(600, 2400)
        db.upsert_daily(pid, d.isoformat(), {
            "steps": max(1500, int(rnd.gauss(7000 if weekend else 9500, 2500))),
            "resting_hr": int(round(56 + 2 * math.sin(i / 9) + rnd.gauss(0, 1.2))),
            "avg_stress": rnd.randint(18, 45),
            "body_battery_high": rnd.randint(60, 100),
            "body_battery_low": rnd.randint(5, 30),
            "active_kcal": rnd.randint(250, 900),
            "total_kcal": rnd.randint(2200, 3100),
            "intensity_min": rnd.randint(0, 70),
            "floors": rnd.randint(2, 20),
            "sleep_s": deep + rem + light,
            "deep_s": deep, "light_s": light, "rem_s": rem, "awake_s": awake,
            "sleep_score": rnd.randint(58, 92),
            "hrv_avg": int(round(48 + 6 * math.sin(i / 11) + rnd.gauss(0, 3))),
        })
        if rnd.random() < 0.8:  # weighs in most mornings
            weight += rnd.gauss(-0.03, 0.25)
            fat += rnd.gauss(-0.015, 0.15)
            ts = int(datetime.combine(d, time(6, rnd.randint(30, 59))).timestamp())
            db.upsert_body(pid, ts, "demo", {
                "weight_kg": round(weight, 2), "body_fat": round(fat, 1),
                "bmi": round(weight / 1.80 ** 2, 1), "water": round(55 - (fat - 24) * 0.6, 1),
                "muscle": round(45 - (fat - 24) * 0.4, 1), "visceral_fat": round(9 + (fat - 24) * 0.3, 1),
                "bmr": round(1750 + (weight - 86) * 20), "metabolic_age": 38,
            })
        if rnd.random() < 0.45:
            kind, title, dist, dur = rnd.choice(ACTIVITIES)
            db.upsert_activity(pid, {
                "activity_id": f"demo-{pid}-{i}", "start_time": f"{d.isoformat()} 07:15:00",
                "name": title, "type": kind, "duration_s": dur * rnd.uniform(0.8, 1.3),
                "distance_m": dist * rnd.uniform(0.8, 1.3) if dist else None,
                "calories": rnd.randint(200, 750), "avg_hr": rnd.randint(105, 158),
            })
    return pid
