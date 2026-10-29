"""Background scheduler: keeps the FCC ULS hams table fresh (each weekly
extract, soon after the FCC publishes it)."""

import logging
import threading
import time

from app.config import get_settings
from app.integrations import uls

log = logging.getLogger(__name__)


def _derived_tables_due() -> bool:
    try:
        return any(uls.derived_tables_due())
    except Exception:
        log.warning("[uls] could not check the derived tables", exc_info=True)
        return False


def start_uls_scheduler() -> threading.Thread | None:
    """Also builds the derived tables (cluster aggregate, places, ZIP
    anchors) of a database upgraded from a version without them, before its
    first import — in the import thread, so the two never overlap — or on
    their own thread when imports are switched off."""
    s = get_settings()
    if not s.uls_import_enabled:
        log.info("[uls] import disabled via ULS_IMPORT_ENABLED")
        if _derived_tables_due():
            threading.Thread(target=uls.ensure_derived_tables, daemon=True,
                             name="uls-derived-tables").start()
        return None

    def loop():
        if _derived_tables_due():
            uls.ensure_derived_tables()
        while True:
            complete = True
            try:
                complete = uls.ensure_fresh()
            except Exception:
                complete = False
                log.exception("[uls] refresh check failed")
            # incomplete work (throttled geocoder, failed download) → retry in
            # 30 min; otherwise the next check (by default daily) asks the FCC
            # whether a new extract is out
            time.sleep(3600 * (s.uls_check_interval_hours if complete else 0.5))

    thread = threading.Thread(target=loop, daemon=True, name="uls-scheduler")
    thread.start()
    log.info("[uls] scheduler started (asks the FCC for a new extract every %d h)", s.uls_check_interval_hours)
    return thread


def start_repeaters_scheduler() -> threading.Thread | None:
    """Keeps the repeater directory fresh. repeaters.ensure_fresh() decides
    which source is due (HearHam weekly, BrandMeister daily, IRLP ~12 h)."""
    s = get_settings()
    if not s.repeaters_enabled:
        log.info("[repeaters] disabled via REPEATERS_ENABLED")
        return None
    from app.integrations import repeaters

    def loop():
        time.sleep(20)  # let startup finish before the first fetch
        while True:
            done = True
            try:
                done = repeaters.ensure_fresh()
            except Exception:
                done = False
                log.exception("[repeaters] refresh failed")
            # due sources are re-checked hourly; failures retry in 15 min
            time.sleep(3600 if done else 900)

    thread = threading.Thread(target=loop, daemon=True, name="repeaters-scheduler")
    thread.start()
    log.info("[repeaters] scheduler started")
    return thread
