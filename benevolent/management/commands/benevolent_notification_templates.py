"""Install the default notification templates (Phase 7), the same idempotent
pattern as benevolent_profiles: safe to run repeatedly, never overwrites an
edit a treasurer has already made unless --force is given.

    python manage.py benevolent_notification_templates [--force]
"""
from django.core.management.base import BaseCommand

from benevolent.services.notify import install_default_templates


class Command(BaseCommand):
    help = "Install the default benevolent notification templates."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Overwrite existing templates with the defaults too.")

    def handle(self, *args, **opts):
        n = install_default_templates(force=opts["force"])
        if opts["force"]:
            self.stdout.write(self.style.SUCCESS("All templates reset to their defaults."))
        else:
            self.stdout.write(self.style.SUCCESS(f"{n} new template(s) installed."))
