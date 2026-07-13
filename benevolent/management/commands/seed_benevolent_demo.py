"""Seed benevolent test data on its own, without running the rest of the
demo (`seed_demo`) first.

Useful for a developer working only on this module, or for a quick,
isolated test/CI fixture — safe to run whether or not `seed_demo` has
already populated the wider application: if a treasurer/assistant/auditor
already exist, they are reused rather than duplicated, and if any
BenevolentScheme already exists, this is a no-op (the underlying seed
chain already guards against re-seeding).

Deliberately NOT a reimplementation: this calls straight into
`seed_demo.Command`'s own, already-built, ten-phases'-worth of benevolent
seed methods (`_seed_benevolent` through `_seed_benevolent_phase9`), which
cascade through each other in sequence — one call here runs the whole
chain, exactly as `seed_demo` itself does. Duplicating that logic in a
second place would be exactly the kind of drift this project's own
principles argue against.

    python manage.py seed_benevolent_demo
"""
import datetime as dt

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from django.db import transaction as db_tx

from core.roles import ASSISTANT, AUDITOR, TREASURER
from core.utils import last_saturday, sabbath_of


class Command(BaseCommand):
    help = "Seed benevolent module demo/test data on its own."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Seed even if BenevolentScheme rows already exist (creates "
                "additional data alongside them, rather than skipping).")

    @db_tx.atomic
    def handle(self, *args, **opts):
        from benevolent.models import BenevolentScheme
        from core.management.commands.seed_demo import Command as SeedDemoCommand

        if BenevolentScheme.objects.exists() and not opts["force"]:
            self.stdout.write(self.style.WARNING(
                "Benevolent data already exists — nothing to do. Pass --force "
                "to seed more alongside it, or use `seed_demo` for a full reset."))
            return

        treasurer = self._user("treasurer", "Tabitha", "Treasurer", "treasurer123",
                               TREASURER, superuser=True)
        self._user("assistant", "Alan", "Assistant", "assistant123", ASSISTANT)
        self._user("auditor", "Aisha", "Auditor", "auditor123", AUDITOR)
        sab = sabbath_of(last_saturday())

        # _seed_benevolent's whole cascade (phases 1-9) picks its members from
        # the EXISTING pool of active members — it has never created its own,
        # because in the full `seed_demo` flow a general church roster always
        # exists first. Running standalone, without that roster, needs one
        # created here — comfortably more than the largest single slice any
        # phase takes (household seeding needs a pool of more than 9).
        self._member_pool()

        seeder = SeedDemoCommand()
        seeder.stdout = self.stdout
        seeder.style = self.style
        seeder._seed_benevolent(treasurer, sab)

        self.stdout.write(self.style.SUCCESS(
            "\nBenevolent demo data ready. Sign in at /benevolent/ with:\n"
            "  treasurer / treasurer123   (full access)\n"
            "  assistant / assistant123   (data entry)\n"
            "  auditor   / auditor123     (read-only)\n"
            "or one of the role-specific demo users seeded alongside them "
            "(ben_admin, ben_approver, ben_committee, ben_registrar, "
            "ben_case_officer, ben_finance, ben_auditor — each password "
            "'<username>123')."))

    def _member_pool(self):
        from members.models import Member
        if Member.objects.filter(active=True).count() >= 20:
            return
        names = [
            "Grace Wanjiru", "Peter Otieno", "Mary Achieng", "John Kamau",
            "Faith Nyambura", "Samuel Mwangi", "Esther Adhiambo", "David Kiptoo",
            "Ruth Wambui", "Joseph Ochieng", "Sarah Njeri", "Daniel Kipchoge",
            "Rebecca Auma", "Michael Njoroge", "Naomi Chebet", "Isaac Mutua",
            "Hannah Wairimu", "Stephen Omondi", "Lydia Moraa", "Paul Kariuki",
        ]
        for i, name in enumerate(names):
            Member.objects.get_or_create(
                name=name.upper(),
                defaults=dict(phone=f"25470{i:07d}", active=True))

    def _user(self, username, first, last, pw, role, superuser=False):
        u, created = User.objects.get_or_create(
            username=username,
            defaults=dict(first_name=first, last_name=last,
                          is_staff=superuser, is_superuser=superuser))
        if created:
            u.set_password(pw)
            u.save()
        if not superuser:
            u.groups.set([Group.objects.get_or_create(name=role)[0]])
        return u
