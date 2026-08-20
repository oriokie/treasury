"""The held-gifts queue has to be reachable from the sidebar.

The screen that releases feed gifts was built first; without a nav link the
only path to /statements/held/ was typing the URL or clicking a register pill
that only appears when held rows happen to be on the current page.
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import TREASURER


class HeldGiftsNavTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tr", password="x",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.user)

    def test_sidebar_links_to_the_held_gifts_queue(self):
        r = self.client.get("/")
        self.assertContains(r, reverse("held_gifts_review"))
        self.assertContains(r, "Gifts awaiting confirmation")
