from django.apps import AppConfig


class PledgesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pledges"

    def ready(self):
        # Wire the listener that keeps pledge payment links honest when a
        # contribution is reversed, reallocated, or moved to another member.
        from . import signals  # noqa: F401
