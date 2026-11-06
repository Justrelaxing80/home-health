"""Streak counting shared by supplements and peptides.

A day's status is "done" (everything scheduled was taken), "missed", or None
(nothing scheduled - such days neither count nor break a streak). Today is still
in progress, so an unfinished today doesn't break the streak; it just isn't added.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable


def compute(status: Callable[[date], str | None], today: date, earliest: date | None) -> dict:
    if earliest is None or earliest > today:
        return {"current": 0, "best": 0, "today": None}
    earliest = max(earliest, today - timedelta(days=730))
    days = []
    d = earliest
    while d <= today:
        days.append((d, status(d)))
        d += timedelta(days=1)

    best = run = 0
    for d, s in days:
        if s == "done":
            run += 1
            best = max(best, run)
        elif s == "missed" and d != today:
            run = 0

    current = 0
    for d, s in reversed(days):
        if s == "done":
            current += 1
        elif s == "missed" and d != today:
            break
    return {"current": current, "best": best, "today": days[-1][1]}
