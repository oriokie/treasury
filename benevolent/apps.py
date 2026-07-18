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
        # Phase 8: register this module's report components and ready-to-use
        # reports with the shared Generic Report Engine — the same "plug in by
        # registration" pattern reports/board_pack_components.py and
        # reports/intelligence_components.py already use for their own
        # sections, so benevolent contributes to the report catalogue the
        # same way any other module would.
        try:
            from .report_components import register_components, register_reports
            register_components()
            register_reports()
            # item 6: the fourteen reporting-gap reports, registered the same way
            from .report_components_extra import (
                register_components as register_extra_components,
                register_reports as register_extra_reports)
            register_extra_components()
            register_extra_reports()
        except Exception:  # noqa: BLE001 — never block startup on the catalogue
            pass
