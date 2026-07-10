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
        from . import consistency_reports
        consistency_reports.register_report()
        # Intelligence-backed components + the comprehensive Treasurer's Report.
        from . import intelligence_components
        intelligence_components.register_components()
        from . import treasurer_report  # noqa: F401
        # Admin-designed report definitions are compiled and registered lazily on
        # first request (see EngineReportView), NOT at startup — this avoids a
        # database query during app initialisation while keeping designed reports
        # available at their URLs.
