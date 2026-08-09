"""Both settings screens under Administration have to be in the menu.

Envelope columns and Allocation priority were built minutes apart, in the same
session, and both are treasurer-only pages that configure how money gets
recorded. Allocation priority got a sidebar link. Envelope columns did not, and
nothing else in the app links to it either — so the view worked, its own twelve
tests passed, and the only way a treasurer could ever reach the screen was by
typing /settings/envelope-columns/ into the address bar.

That is the failure `EveryBuiltScreenIsReachableTests` in core/test_nav_audit.py
was written for: working code no user can arrive at. This is the same check
narrowed to the pair, because the pair is what makes it obvious — one of two
siblings in the same menu group is a link and the other is not.
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import TREASURER


class AdministrationMenuTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("navset_tr", password="navset-pass-1",
                                           is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.tr)

    def _sidebar(self):
        return self.client.get(reverse("dashboard")).content.decode()

    def test_envelope_columns_is_in_the_menu(self):
        self.assertIn(
            reverse("envelope_columns"), self._sidebar(),
            "The envelope-columns settings page is built, gated and tested but "
            "appears in no menu, so a treasurer cannot reach it without typing "
            "the URL.")

    def test_it_sits_with_the_sibling_it_was_built_alongside(self):
        """Both or neither: a lone link is how the other one went missing."""
        body = self._sidebar()
        self.assertIn(reverse("allocation_priority"), body)
        self.assertIn(reverse("envelope_columns"), body)

    def test_the_link_is_treasurer_only_like_the_page_it_opens(self):
        """A menu entry that everyone sees for a page only treasurers may open
        is just a link to a permission error."""
        clerk = User.objects.create_user("navset_clerk", password="navset-pass-2")
        client = Client()
        client.force_login(clerk)
        self.assertNotIn(reverse("envelope_columns"),
                         client.get(reverse("dashboard")).content.decode())
