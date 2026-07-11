from django.apps import AppConfig


class BenevolentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "benevolent"
    verbose_name = "Benevolent scheme engine"

    def ready(self):
        from . import signals  # noqa: F401
        # register this module's figures in the Financial Metrics Registry, so
        # every benevolent number in the app is discoverable and defined in one
        # place, like every other financial concept
        try:
            from .metrics import register_metrics
            register_metrics()
        except Exception:  # noqa: BLE001 — never block startup on the catalogue
            pass
