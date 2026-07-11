"""Install the built-in policy profile library. Idempotent: a profile a church has
since edited is left exactly as they left it — a tuned profile is theirs, and must
not be quietly reset by an upgrade."""
from django.core.management.base import BaseCommand

from benevolent.services.profiles import install_builtins


class Command(BaseCommand):
    help = "Install the built-in benevolent policy profiles."

    def handle(self, *args, **opts):
        n = install_builtins()
        self.stdout.write(self.style.SUCCESS(
            f"{n} built-in profile(s) installed."
            if n else "Built-in profiles already present; nothing to do."))
