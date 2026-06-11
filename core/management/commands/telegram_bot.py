"""Run the Telegram bot by long-polling — for deployments without a public
HTTPS URL for a webhook. Usage:

    python manage.py telegram_bot

Leave it running (e.g. under systemd/supervisor). It calls getUpdates in a loop
and dispatches each update through the same handler the webhook uses. Stop with
Ctrl-C. Requires telegram_enabled and a bot token in Settings.
"""
import json
import time
import urllib.request
import urllib.parse

from django.core.management.base import BaseCommand

from core.models import SiteConfig
from core.services import telegram_bot


class Command(BaseCommand):
    help = "Run the Telegram treasury bot via long-polling."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="Process the current backlog once and exit (for testing).")

    def handle(self, *args, **opts):
        cfg = SiteConfig.get()
        token = cfg.telegram_bot_token
        if not cfg.telegram_enabled or not token:
            self.stderr.write("Telegram is disabled or no bot token is set (Settings → Telegram).")
            return
        self.stdout.write(self.style.SUCCESS("Telegram bot polling… (Ctrl-C to stop)"))
        offset = None
        while True:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
            try:
                with urllib.request.urlopen(url, timeout=40) as r:
                    payload = json.loads(r.read().decode())
            except Exception as exc:  # network hiccup — back off and retry
                self.stderr.write(f"poll error: {exc}")
                time.sleep(3)
                continue
            for upd in payload.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    telegram_bot.process_and_reply(upd)
                except Exception as exc:  # noqa
                    self.stderr.write(f"handler error: {exc}")
            if opts.get("once"):
                self.stdout.write("Processed backlog once; exiting.")
                return
