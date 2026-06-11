"""In-app Telegram poller.

Runs the bot inside the main Django process as a daemon thread, so a simple
deployment needs no separate worker or public webhook. Enable it with the
`telegram_run_in_app` setting. The thread long-polls getUpdates and feeds each
update through the same `process_and_reply` used by the webhook, so behaviour is
identical either way. A module-level guard ensures only one poller runs per
process (so the autoreloader's two processes don't both poll)."""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)
_poller_started = False
_lock = threading.Lock()


def _poll_loop():
    from core.models import SiteConfig
    from core.services.telegram_bot import process_and_reply
    offset = None
    backoff = 1
    while True:
        try:
            cfg = SiteConfig.get()
            if not (cfg.telegram_enabled and cfg.telegram_run_in_app):
                time.sleep(10)
                continue
            token = (cfg.telegram_bot_token or "").strip()
            if not token:
                time.sleep(15)
                continue
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            url = (f"https://api.telegram.org/bot{token}/getUpdates?"
                   + urllib.parse.urlencode(params))
            with urllib.request.urlopen(url, timeout=35) as r:
                payload = json.loads(r.read().decode())
            backoff = 1
            for upd in payload.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    process_and_reply(upd)
                except Exception:  # noqa: BLE001
                    logger.exception("telegram update failed")
        except Exception:  # noqa: BLE001
            time.sleep(min(backoff, 60))
            backoff = min(backoff * 2, 60)


def start_in_app_poller():
    """Start the daemon poller once per process, if enabled in settings."""
    global _poller_started
    with _lock:
        if _poller_started:
            return
        try:
            from core.models import SiteConfig
            cfg = SiteConfig.get()
        except Exception:  # DB not ready (e.g. during migrate)
            return
        if not (cfg.telegram_enabled and cfg.telegram_run_in_app):
            return
        _poller_started = True
        t = threading.Thread(target=_poll_loop, name="telegram-poller", daemon=True)
        t.start()
        logger.info("In-app Telegram poller started")
