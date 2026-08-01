"""Which fund columns a new Sabbath envelope sheet opens with.

The list was a constant in the source — `envelopes.services.posting.PREFERRED`,
five fund names chosen when the app was written. A church that collects under
different headings had no way to say so, so someone re-picked the columns by
hand on every new sheet, every Sabbath, for as long as they had been using it.

Only the OPENING state is configured. Every fund stays available on the sheet,
so nothing here can stop money reaching a fund — a column left out can still be
added on the day. That is what makes this safe to change.
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.models import SiteConfig
from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from envelopes.services.posting import (PREFERRED, column_catalog,
                                        configured_default_keys)

URL = "/settings/envelope-columns/"


class _Funds(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("ec_tr", password="ec-pass-1",
                                           is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.a = Department.objects.create(
            name="EcAlpha", slug="ec-alpha", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.b = Department.objects.create(
            name="EcBeta", slug="ec-beta", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.client = Client()
        self.client.force_login(self.tr)

    def _defaults(self):
        return [c["label"] for c in column_catalog() if c["default"]]


class DefaultsTests(_Funds):
    def test_without_configuration_the_built_in_list_is_used(self):
        self.assertEqual(configured_default_keys(), [])
        # PREFERRED names may not all exist in a bare test database; what must
        # hold is that the built-in path is what decides.
        self.assertTrue(all(any(p.lower() == c["name"].lower() for p in PREFERRED)
                            for c in column_catalog() if c["default"]))

    def test_a_configured_list_replaces_it(self):
        cfg = SiteConfig.get()
        cfg.envelope_default_funds = str(self.b.id)
        cfg.save()
        self.assertEqual(self._defaults(), ["EcBeta"])

    def test_the_configured_order_is_the_column_order(self):
        cfg = SiteConfig.get()
        cfg.envelope_default_funds = f"{self.b.id}\n{self.a.id}"
        cfg.save()
        self.assertEqual([c["label"] for c in column_catalog()[:2]],
                         ["EcBeta", "EcAlpha"])

    def test_a_fund_that_no_longer_exists_is_ignored(self):
        """Config outlives the funds it names. A deleted fund must not leave a
        blank column on a sheet somebody is entering money into."""
        cfg = SiteConfig.get()
        cfg.envelope_default_funds = f"999999\n{self.a.id}"
        cfg.save()
        self.assertEqual(self._defaults(), ["EcAlpha"])

    def test_a_duplicate_key_is_kept_once(self):
        cfg = SiteConfig.get()
        cfg.envelope_default_funds = f"{self.a.id}\n{self.a.id}"
        cfg.save()
        self.assertEqual(self._defaults(), ["EcAlpha"])

    def test_unreadable_configuration_never_breaks_the_grid(self):
        """This builds the entry grid — a bad setting must not be able to stop
        a Sabbath's envelopes being entered."""
        cfg = SiteConfig.get()
        cfg.envelope_default_funds = "\n\n,,,\n"
        cfg.save()
        self.assertEqual(configured_default_keys(), [])
        self.assertTrue(column_catalog())


class PageTests(_Funds):
    def test_it_opens(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Envelope columns")

    def test_it_offers_every_fund(self):
        r = self.client.get(URL)
        labels = ([c["label"] for c in r.context["selected"]]
                  + [c["label"] for c in r.context["available"]])
        self.assertIn("EcAlpha", labels)
        self.assertIn("EcBeta", labels)

    def test_a_choice_is_saved(self):
        self.client.post(URL, {"columns": [str(self.b.id), str(self.a.id)]})
        self.assertEqual(configured_default_keys(),
                         [str(self.b.id), str(self.a.id)])

    def test_an_empty_choice_is_refused(self):
        """A sheet that opens with no columns at all is not a configuration,
        it is a mistake nobody would notice until the next Sabbath."""
        self.client.post(URL, {"columns": [str(self.a.id)]})
        r = self.client.post(URL, {"columns": []})
        self.assertContains(r, "at least one column")
        self.assertEqual(configured_default_keys(), [str(self.a.id)])

    def test_an_unknown_key_is_dropped_rather_than_stored(self):
        self.client.post(URL, {"columns": ["not-a-fund", str(self.a.id)]})
        self.assertEqual(configured_default_keys(), [str(self.a.id)])

    def test_it_can_be_reset(self):
        self.client.post(URL, {"columns": [str(self.a.id)]})
        self.client.post(URL, {"action": "reset"})
        self.assertEqual(configured_default_keys(), [])

    def test_it_says_the_sheet_keeps_every_fund_available(self):
        """The sentence that makes this setting safe to change."""
        self.assertContains(self.client.get(URL), "stays available")


class PermissionTests(_Funds):
    def test_a_clerk_cannot_change_it(self):
        clerk = User.objects.create_user("ec_clerk", password="ec-pass-2")
        clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        c = Client()
        c.force_login(clerk)
        r = c.post(URL, {"columns": [str(self.a.id)]})
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(configured_default_keys(), [])
