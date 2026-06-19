"""Tests for the configurable profile/rights system layered on role groups."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from core import rights as R
from core.rights import has_right, user_rights, display_phone
from accounts.models import Profile
from members.models import Member


def _user(username, group=None, superuser=False):
    u = User.objects.create_user(username=username, password="x", is_superuser=superuser,
                                 is_staff=superuser)
    if group:
        g, _ = Group.objects.get_or_create(name=group)
        u.groups.add(g)
    return u


class RightsResolutionTests(TestCase):
    def test_superuser_has_every_right(self):
        u = _user("admin", superuser=True)
        self.assertEqual(user_rights(u), set(R.RIGHT_KEYS))
        self.assertTrue(has_right(u, "manage_profiles"))

    def test_group_fallback_when_no_profile(self):
        t = _user("tina", group=R.roles.TREASURER)
        a = _user("amos", group=R.roles.ASSISTANT)
        au = _user("ann", group=R.roles.AUDITOR)
        # treasurer fallback = everything
        self.assertEqual(user_rights(t), set(R.RIGHT_KEYS))
        # assistant can enter data + see identities, but cannot approve/remit
        self.assertTrue(has_right(a, "record_expenses"))
        self.assertTrue(has_right(a, "view_member_phone_full"))
        self.assertFalse(has_right(a, "approve_expenses"))
        self.assertFalse(has_right(a, "manage_profiles"))
        # auditor read-only
        self.assertTrue(has_right(au, "view_reports"))
        self.assertTrue(has_right(au, "view_audit"))
        self.assertFalse(has_right(au, "record_expenses"))

    def test_profile_overrides_group_and_can_restrict(self):
        # a treasurer-group user assigned a restrictive profile is bound by it
        u = _user("rick", group=R.roles.TREASURER)
        p = Profile.objects.create(name="Restricted", rights=["view_reports"])
        p.users.add(u)
        self.assertEqual(user_rights(u), {"view_reports"})
        self.assertFalse(has_right(u, "approve_expenses"))
        self.assertFalse(has_right(u, "view_member_phone_full"))

    def test_union_of_multiple_profiles(self):
        u = _user("uli")
        Profile.objects.create(name="P1", rights=["view_reports"]).users.add(u)
        Profile.objects.create(name="P2", rights=["export_reports", "view_audit"]).users.add(u)
        self.assertEqual(user_rights(u), {"view_reports", "export_reports", "view_audit"})

    def test_unknown_rights_are_dropped(self):
        p = Profile.objects.create(name="Bad", rights=["view_reports", "not_a_right"])
        self.assertEqual(set(p.rights), {"view_reports"})   # cleaned on save
        u = _user("uma"); p.users.add(u)
        self.assertEqual(user_rights(u), {"view_reports"})

    def test_anonymous_has_no_rights(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(user_rights(AnonymousUser()), set())


class PhoneMaskingTests(TestCase):
    def setUp(self):
        self.m = Member.objects.create(name="Ruth Momanyi", phone="254712345678")

    def test_display_phone_full_vs_masked(self):
        full = _user("tina", group=R.roles.TREASURER)
        self.assertEqual(display_phone(full, "254712345678"), "254712345678")
        restricted = _user("len")
        Profile.objects.create(name="NoPhone", rights=["view_reports"]).users.add(restricted)
        masked = display_phone(restricted, "254712345678")
        self.assertNotEqual(masked, "254712345678")
        self.assertTrue(masked.endswith("678"))
        self.assertIn("*", masked)

    def test_member_search_masks_for_restricted(self):
        restricted = _user("len2", group=R.roles.ASSISTANT)   # can use the endpoint (group)
        Profile.objects.create(name="NoPhone2", rights=["record_giving"]).users.add(restricted)
        c = Client(); c.force_login(restricted)
        r = c.get("/members/search/?q=Ruth")
        self.assertEqual(r.status_code, 200)
        phone = r.json()["results"][0]["phone"]
        self.assertNotEqual(phone, "254712345678")
        self.assertIn("*", phone)

    def test_member_search_full_for_treasurer(self):
        t = _user("tina2", group=R.roles.TREASURER)
        c = Client(); c.force_login(t)
        r = c.get("/members/search/?q=Ruth")
        self.assertEqual(r.json()["results"][0]["phone"], "254712345678")

    def test_member_list_page_masks_for_restricted(self):
        restricted = _user("len3", group=R.roles.AUDITOR)   # read access
        Profile.objects.create(name="NoPhone3", rights=["view_reports"]).users.add(restricted)
        c = Client(); c.force_login(restricted)
        body = c.get("/members/").content.decode()
        self.assertNotIn("254712345678", body)


class ProfileManagementTests(TestCase):
    def test_gate_requires_manage_profiles(self):
        # assistant (no manage_profiles) is redirected away
        a = _user("amos", group=R.roles.ASSISTANT)
        c = Client(); c.force_login(a)
        r = c.get("/profiles/")
        self.assertEqual(r.status_code, 302)
        # treasurer can view
        t = _user("tina", group=R.roles.TREASURER)
        c2 = Client(); c2.force_login(t)
        self.assertEqual(c2.get("/profiles/").status_code, 200)

    def test_create_and_assign_profile(self):
        t = _user("tina", group=R.roles.TREASURER)
        target = _user("newbie", group=R.roles.ASSISTANT)
        c = Client(); c.force_login(t)
        r = c.post("/profiles/new/", {
            "name": "Counter", "description": "Counts only",
            "right_count_envelopes": "on", "right_view_reports": "on",
            "users": [str(target.id)],
        })
        self.assertEqual(r.status_code, 302)
        p = Profile.objects.get(name="Counter")
        self.assertEqual(set(p.rights), {"count_envelopes", "view_reports"})
        self.assertIn(target, p.users.all())
        # the assigned user is now bound by the profile
        self.assertEqual(user_rights(target), {"count_envelopes", "view_reports"})

    def test_cannot_delete_system_profile(self):
        t = _user("tina", group=R.roles.TREASURER)
        sysp = Profile.objects.filter(is_system=True).first()
        c = Client(); c.force_login(t)
        c.post(f"/profiles/{sysp.id}/delete/")
        self.assertTrue(Profile.objects.filter(pk=sysp.id).exists())

    def test_delete_custom_profile(self):
        t = _user("tina", group=R.roles.TREASURER)
        p = Profile.objects.create(name="Temp", rights=["view_reports"])
        c = Client(); c.force_login(t)
        c.post(f"/profiles/{p.id}/delete/")
        self.assertFalse(Profile.objects.filter(pk=p.id).exists())


class BackwardCompatTests(TestCase):
    def test_existing_treasurer_unaffected(self):
        t = _user("tina", group=R.roles.TREASURER)
        # no profile assigned -> full rights via group fallback, incl. full phones
        self.assertTrue(has_right(t, "approve_expenses"))
        self.assertTrue(has_right(t, "view_member_phone_full"))
        self.assertEqual(display_phone(t, "254700111222"), "254700111222")

    def test_default_profiles_exist(self):
        # migration seeded the four default profiles
        names = set(Profile.objects.filter(is_system=True).values_list("name", flat=True))
        self.assertTrue({"Treasurer (default)", "Assistant (default)",
                         "Auditor (default)", "Leader (default)"} <= names)
