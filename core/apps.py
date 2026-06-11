import os
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Only start the in-app Telegram poller in a real server process, never
        # during migrations/tests/shell. RUN_MAIN guards the autoreloader's
        # double-start in development.
        import sys
        if any(c in sys.argv for c in ("migrate", "makemigrations", "test",
                                       "collectstatic", "shell", "loaddata",
                                       "dumpdata", "seed_demo", "import_legacy")):
            return
        if os.environ.get("RUN_MAIN") == "false":
            return
        try:
            from core.services.telegram_poller import start_in_app_poller
            start_in_app_poller()
        except Exception:
            pass
