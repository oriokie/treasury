"""The standalone `seed_benevolent_demo` command — seeds benevolent test
data on its own, without requiring the full `seed_demo` to have run first.
"""
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase


class SeedBenevolentDemoTests(TestCase):

    def test_running_on_an_empty_database_creates_real_data(self):
        from benevolent.models import BenevolentScheme, SchemeMembership, BenevolentCase
        self.assertEqual(BenevolentScheme.objects.count(), 0)
        call_command("seed_benevolent_demo")
        self.assertGreater(BenevolentScheme.objects.count(), 0)
        self.assertGreater(SchemeMembership.objects.count(), 0)
        self.assertGreater(BenevolentCase.objects.count(), 0)

    def test_creates_the_three_standard_demo_users(self):
        call_command("seed_benevolent_demo")
        for username in ("treasurer", "assistant", "auditor"):
            self.assertTrue(User.objects.filter(username=username).exists())

    def test_creates_the_seven_role_specific_demo_users(self):
        call_command("seed_benevolent_demo")
        for username in ("ben_admin", "ben_approver", "ben_committee",
                         "ben_registrar", "ben_case_officer", "ben_finance",
                         "ben_auditor"):
            self.assertTrue(User.objects.filter(username=username).exists(), username)

    def test_running_twice_is_a_safe_no_op(self):
        from benevolent.models import BenevolentScheme
        call_command("seed_benevolent_demo")
        first_count = BenevolentScheme.objects.count()
        call_command("seed_benevolent_demo")
        self.assertEqual(BenevolentScheme.objects.count(), first_count)

    def test_reuses_an_existing_treasurer_rather_than_duplicating(self):
        User.objects.create_user("treasurer", password="alreadyhere")
        call_command("seed_benevolent_demo")
        self.assertEqual(User.objects.filter(username="treasurer").count(), 1)

    def test_the_seeded_scheme_has_a_published_policy_and_active_members(self):
        from benevolent.models import BenevolentScheme, SchemeMembership
        call_command("seed_benevolent_demo")
        scheme = BenevolentScheme.objects.get(code="BEN")
        self.assertIsNotNone(scheme.current_policy)
        self.assertTrue(scheme.memberships.filter(
            status=SchemeMembership.Status.ACTIVE).exists())

    def test_the_seeded_case_has_actually_been_paid(self):
        from benevolent.models import BenevolentCase
        call_command("seed_benevolent_demo")
        self.assertTrue(BenevolentCase.objects.filter(status="PAID").exists())

    def test_works_fine_after_the_full_seed_demo_has_already_run(self):
        """Real-world compatibility: a developer who already ran the full
        demo seed should be able to run this too without error, and it
        should recognise the existing data rather than duplicate it."""
        from benevolent.models import BenevolentScheme
        call_command("seed_demo")
        count_after_full_seed = BenevolentScheme.objects.count()
        self.assertGreater(count_after_full_seed, 0)
        call_command("seed_benevolent_demo")   # must not error or duplicate
        self.assertEqual(BenevolentScheme.objects.count(), count_after_full_seed)
