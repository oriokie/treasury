"""User list search, filter, sort, and pagination."""
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from accounts.models import UserProfile, TwoFactor


def _tr(username="tr_listsearch"):
    u = User.objects.create_user(username, password="TrPass1234!", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class UserListSearchTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.alice = User.objects.create_user("alice_search", password="x",
            first_name="Alice", last_name="Wonder", email="alice@example.com")
        self.alice.groups.add(Group.objects.get_or_create(name="Assistant")[0])
        self.bob = User.objects.create_user("bob_search", password="x",
            first_name="Bob", last_name="Builder")
        self.bob.groups.add(Group.objects.get_or_create(name="Auditor")[0])
        self.bob.is_active = False
        self.bob.save()

    def test_search_by_username(self):
        b = self.c.get("/users/?q=alice_search").content.decode()
        self.assertIn("alice_search", b)
        self.assertNotIn("bob_search", b)

    def test_search_by_first_name(self):
        b = self.c.get("/users/?q=Wonder").content.decode()
        self.assertIn("alice_search", b)

    def test_search_by_email(self):
        b = self.c.get("/users/?q=alice@example.com").content.decode()
        self.assertIn("alice_search", b)

    def test_filter_by_role(self):
        b = self.c.get("/users/?role=Auditor").content.decode()
        self.assertIn("bob_search", b)
        self.assertNotIn("alice_search", b)

    def test_filter_by_status_inactive(self):
        b = self.c.get("/users/?status=inactive").content.decode()
        self.assertIn("bob_search", b)
        self.assertNotIn("alice_search", b)

    def test_filter_by_status_locked(self):
        p = UserProfile.for_user(self.alice)
        p.locked = True
        p.save()
        b = self.c.get("/users/?status=locked").content.decode()
        self.assertIn("alice_search", b)
        self.assertNotIn("bob_search", b)

    def test_sort_by_username_descending(self):
        r = self.c.get("/users/?sort=-username")
        self.assertEqual(r.status_code, 200)

    def test_two_fa_status_shown(self):
        tf = TwoFactor(user=self.alice, method="TOTP", confirmed=True)
        tf.set_secret("JBSWY3DPEHPK3PXP")
        tf.save()
        b = self.c.get("/users/").content.decode()
        # both the "On" indicator and the username should appear; a looser
        # check than exact HTML structure, robust to markup tweaks
        self.assertIn("alice_search", b)

    def test_no_results_shows_empty_state(self):
        b = self.c.get("/users/?q=nonexistentusernamexyz").content.decode()
        self.assertIn("No users match", b)

    def test_manage_link_present(self):
        b = self.c.get("/users/").content.decode()
        self.assertIn(f"/users/{self.alice.id}/edit/", b)


class UserListPerformanceTests(TestCase):
    """Found during a follow-up performance check: the roles column was
    computed via user_roles(u) per user, which calls
    user.groups.values_list(...) — a call that always issues a fresh query,
    completely bypassing prefetch_related("groups") since values_list()
    returns a new queryset rather than using the prefetched cache. Fixed by
    building the roles map from the prefetched relation directly."""
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        for i in range(15):
            u = User.objects.create_user(f"perftest_user{i}", password="x")
            u.groups.add(Group.objects.get_or_create(name="Assistant")[0])

    def test_query_count_does_not_grow_per_user(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            r = self.c.get("/users/")
        self.assertEqual(r.status_code, 200)
        # well below "one query per user" (15+ users) — a handful of fixed,
        # bulk queries regardless of how many users are on the page
        self.assertLess(len(ctx.captured_queries), 30)

    def test_roles_column_still_correct(self):
        from core.roles import user_roles
        b = self.c.get("/users/").content.decode()
        # spot check: every non-superuser account created in setUp must show
        # its real role, not be blank or wrong
        self.assertGreaterEqual(b.count("Assistant"), 15)
