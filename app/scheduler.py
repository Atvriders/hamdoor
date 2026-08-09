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
            complete = True
            try:
                complete = uls.ensure_fresh()
            except Exception:
                complete = False
                log.exception("[uls] refresh check failed")
            # incomplete work (throttled geocoder, failed download) → retry in
            # 30 min; otherwise normal daily check
            time.sleep(3600 * (s.uls_check_interval_hours if complete else 0.5))

    thread = threading.Thread(target=loop, daemon=True, name="uls-scheduler")
    thread.start()
    log.info("[uls] scheduler started (refresh every %d days)", s.uls_refresh_days)
    return thread
