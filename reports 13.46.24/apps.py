from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'reports'

    def ready(self):
        # Import engine-registered reports so they populate the report registry
        # at startup. Kept in ready() (not at module import) to avoid import
        # cycles with models during app loading.
        from . import engine_reports  # noqa: F401
        # Register the reusable component library and the component-demo report.
        from core.reporting import component_library  # noqa: F401
        from core.reporting import narrative_library  # noqa: F401
        from . import component_demo  # noqa: F401
        from . import financial_statements  # noqa: F401
        from . import board_report  # noqa: F401
