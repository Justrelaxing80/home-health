"""Runs Garmin/Renpho syncs: one at a time per profile+provider, status kept in the DB."""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, garmin, scale

log = logging.getLogger("home-health.sync")

SYNC_INTERVAL_MIN = int(os.environ.get("SYNC_INTERVAL_MINUTES", "60"))

_pool = ThreadPoolExecutor(max_workers=2)
_running: set[tuple[int, str]] = set()
_lock = threading.Lock()
_scheduler: BackgroundScheduler | None = None

PROVIDERS = {"garmin": garmin.sync, "renpho": scale.sync}


def _set(profile_id: int, provider: str, status: str, error: str | None = None) -> None:
    db.execute(
        "UPDATE connections SET status=?, last_error=? WHERE profile_id=? AND provider=?",
        (status, error, profile_id, provider),
    )


def _run(profile_id: int, provider: str) -> None:
    try:
        _set(profile_id, provider, "syncing")
        result = PROVIDERS[provider](profile_id)
        log.info("sync %s profile=%s ok: %s", provider, profile_id, result)
        _set(profile_id, provider, "linked")
    except garmin.NeedsMfa as e:
        log.warning("garmin profile=%s needs re-link: %s", profile_id, e)
        _set(profile_id, provider, "needs_relink",
             "Garmin session expired and needs a fresh login (possibly an MFA code). Re-link in Settings.")
    except Exception as e:  # noqa: BLE001
        log.exception("sync %s profile=%s failed", provider, profile_id)
        _set(profile_id, provider, "error", str(e)[:500])
    finally:
        with _lock:
            _running.discard((profile_id, provider))


def start(profile_id: int, provider: str | None = None) -> list[str]:
    """Queue syncs for one profile. Returns the providers actually queued."""
    conns = db.rows("SELECT provider FROM connections WHERE profile_id=?", (profile_id,))
    queued = []
    for c in conns:
        p = c["provider"]
        if provider and p != provider:
            continue
        with _lock:
            if (profile_id, p) in _running:
                continue
            _running.add((profile_id, p))
        _pool.submit(_run, profile_id, p)
        queued.append(p)
    return queued


def sync_all() -> None:
    for p in db.rows("SELECT id FROM profiles"):
        start(p["id"])


def start_scheduler() -> None:
    global _scheduler
    if _scheduler or SYNC_INTERVAL_MIN <= 0:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(sync_all, "interval", minutes=SYNC_INTERVAL_MIN, id="sync_all",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    # Recover rows left as "syncing" by a crash/restart.
    db.execute("UPDATE connections SET status='linked' WHERE status='syncing'")


def stop_scheduler() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)
