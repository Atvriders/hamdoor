"""Background scheduler: keeps the FCC ULS hams table fresh (weekly)."""

import logging
import threading
import time

from app.config import get_settings
from app.integrations import uls

log = logging.getLogger(__name__)


def start_uls_scheduler() -> threading.Thread | None:
    s = get_settings()
    if not s.uls_import_enabled:
        log.info("[uls] import disabled via ULS_IMPORT_ENABLED")
        return None

    def loop():
        while True:
            ok = True
            try:
                uls.ensure_fresh()
            except Exception:
                ok = False
                log.exception("[uls] refresh check failed")
            # retry failures sooner than the normal daily check
            time.sleep(3600 * (s.uls_check_interval_hours if ok else 0.5))

    thread = threading.Thread(target=loop, daemon=True, name="uls-scheduler")
    thread.start()
    log.info("[uls] scheduler started (refresh every %d days)", s.uls_refresh_days)
    return thread
