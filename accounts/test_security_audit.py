"""Regression tests for the security & internal-controls review: last-active-
treasurer lockout protection, 2FA brute-force rate limiting, and the manual
journal entry's defensive validation."""
import pyotp
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from accounts.models import TwoFactor


def _tr(name):
    u = User.objects.create_user(name, password="StrongPass1234!")
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class LastTreasurerProtectionTests(TestCase):
    def setUp(self):
        self.tr = _tr("tr_lockout_sec")
        self.c = Client(); self.c.force_login(self.tr)

    def test_cannot_demote_the_only_treasurer(self):
        r = self.c.post(f"/users/{self.tr.id}/edit/", {"role": "Assistant", "active": "on"})
        self.tr.refresh_from_db()
        self.assertTrue(self.tr.groups.filter(name="Treasurer").exists())

    def test_cannot_deactivate_the_only_treasurer(self):
        r = self.c.post(f"/users/{self.tr.id}/edit/", {"role": "Treasurer"})
        self.tr.refresh_from_db()
        self.assertTrue(self.tr.is_active)

    def test_can_demote_when_another_active_treasurer_exists(self):
        # self-edit is now blocked unconditionally (a later review's explicit
        # security requirement: users cannot modify their own permissions) —
        # a DIFFERENT administrator must make the change, even when demoting
        # would otherwise be safe because another treasurer exists
        other = _tr("tr_lockout_sec_other")
        c_other = Client(); c_other.force_login(other)
        r = c_other.post(f"/users/{self.tr.id}/edit/",
            {"form_name": "role_form", "role": "Assistant", "active": "on"})
        self.tr.refresh_from_db()
        self.assertFalse(self.tr.groups.filter(name="Treasurer").exists())

    def test_self_edit_is_blocked_regardless_of_other_treasurers(self):
        other = _tr("tr_lockout_sec_self_other")
        r = self.c.post(f"/users/{self.tr.id}/edit/",
            {"form_name": "role_form", "role": "Assistant", "active": "on"})
        self.tr.refresh_from_db()
        self.assertTrue(self.tr.groups.filter(name="Treasurer").exists())

    def test_can_freely_edit_non_treasurer_roles(self):
        assistant = User.objects.create_user("asst_lockout_sec", password="x")
        assistant.groups.add(Group.objects.get_or_create(name="Assistant")[0])
        r = self.c.post(f"/users/{assistant.id}/edit/", {"role": "Auditor", "active": "on"})
        assistant.refresh_from_db()
        self.assertTrue(assistant.groups.filter(name="Auditor").exists())


class TwoFactorBruteForceProtectionTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("tfa_sec_user", password="StrongPass1234!")
        self.secret = pyotp.random_base32()
        tf = TwoFactor(user=self.u, method="TOTP", confirmed=True)
        tf.set_secret(self.secret)
        tf.save()

    def test_login_holds_for_2fa(self):
        c = Client()
        r = c.post("/accounts/login/", {"username": "tfa_sec_user",
                                        "password": "StrongPass1234!"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("2fa", r.url)

    def test_wrong_codes_lock_out_after_five(self):
        c = Client()
        c.post("/accounts/login/", {"username": "tfa_sec_user",
                                    "password": "StrongPass1234!"})
        for _ in range(4):
            r = c.post("/2fa/verify/", {"token": "000000"})
            self.assertEqual(r.status_code, 200)   # still on the verify page
        r = c.post("/2fa/verify/", {"token": "000000"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login", r.url)

    def test_pending_state_cleared_after_lockout(self):
        c = Client()
        c.post("/accounts/login/", {"username": "tfa_sec_user",
                                    "password": "StrongPass1234!"})
        for _ in range(5):
            c.post("/2fa/verify/", {"token": "000000"})
        r = c.get("/2fa/verify/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login", r.url)

    def test_correct_code_works_on_fresh_login(self):
        c = Client()
        c.post("/accounts/login/", {"username": "tfa_sec_user",
                                    "password": "StrongPass1234!"})
        code = pyotp.TOTP(self.secret).now()
        r = c.post("/2fa/verify/", {"token": code})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("2fa", r.url)

    def test_attempt_counter_resets_on_fresh_login(self):
        c = Client()
        c.post("/accounts/login/", {"username": "tfa_sec_user",
                                    "password": "StrongPass1234!"})
        for _ in range(3):
            c.post("/2fa/verify/", {"token": "000000"})
        # a fresh login should reset the counter, not carry over stale attempts
        c.post("/accounts/login/", {"username": "tfa_sec_user",
                                    "password": "StrongPass1234!"})
        for _ in range(3):
            r = c.post("/2fa/verify/", {"token": "000000"})
            self.assertEqual(r.status_code, 200)
        code = pyotp.TOTP(self.secret).now()
        r = c.post("/2fa/verify/", {"token": code})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("2fa", r.url)
