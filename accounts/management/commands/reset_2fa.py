"""Inspect and reset two-factor enrolments — a backend recovery tool.

Why this exists: a TOTP secret is stored encrypted with a key derived from
TREASURY_ENCRYPTION_KEY (or SECRET_KEY if that isn't set). If that key ever
changes, every enrolled secret becomes unreadable and those users can't pass
two-factor at login. The durable fix is to set a stable TREASURY_ENCRYPTION_KEY
in .env; this command clears affected enrolments so the user can re-enrol.

Examples:
    python manage.py reset_2fa --status          # list enrolments + readability
    python manage.py reset_2fa --all-broken      # clear only unreadable secrets
    python manage.py reset_2fa alice bob          # clear specific users
"""
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model

from accounts.models import TwoFactor


class Command(BaseCommand):
    help = "Inspect or reset two-factor authentication enrolments."

    def add_arguments(self, parser):
        parser.add_argument("usernames", nargs="*",
                            help="Usernames whose 2FA enrolment to clear.")
        parser.add_argument("--status", action="store_true",
                            help="List enrolments and whether each secret is readable.")
        parser.add_argument("--all-broken", action="store_true",
                            help="Clear every enrolment whose secret can't be read.")

    def handle(self, *args, **opts):
        User = get_user_model()
        rows = TwoFactor.objects.select_related("user").all()

        if opts["status"]:
            if not rows:
                self.stdout.write("No two-factor enrolments.")
                return
            self.stdout.write(f"{'User':24} {'Confirmed':10} {'Secret':12}")
            self.stdout.write("-" * 48)
            for tf in rows:
                self.stdout.write(
                    f"{tf.user.get_username():24} "
                    f"{('yes' if tf.confirmed else 'no'):10} "
                    f"{('readable' if tf.secret_readable else 'UNREADABLE'):12}")
            return

        to_clear = []
        if opts["all_broken"]:
            to_clear = [tf for tf in rows if not tf.secret_readable]
            if not to_clear:
                self.stdout.write("No unreadable enrolments — nothing to clear.")
                return
        elif opts["usernames"]:
            for name in opts["usernames"]:
                tf = TwoFactor.objects.filter(user__username=name).first()
                if not tf:
                    self.stderr.write(f"  {name}: no enrolment found")
                    continue
                to_clear.append(tf)
        else:
            raise CommandError("Give one or more usernames, or use --status / "
                               "--all-broken.")

        for tf in to_clear:
            name = tf.user.get_username()
            tf.delete()
            self.stdout.write(self.style.SUCCESS(
                f"  cleared 2FA for {name} — they can re-enrol at /2fa/setup/"))
        self.stdout.write(f"Done — {len(to_clear)} enrolment(s) cleared.")
