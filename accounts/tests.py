"""Coverage for authentication and user management (previously untested)."""
from django.contrib.auth.models import User, Group
from django.test import TestCase, override_settings
from django.urls import reverse

from core.roles import TREASURER, ASSISTANT


def _user(name, role, **kw):
    u = User.objects.create_user(name, password="x", **kw)
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


@override_settings(AXES_ENABLED=False)
class AuthTests(TestCase):
    def test_login_and_dashboard(self):
        u = _user("auth_tr", TREASURER)
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_unauthenticated_protected_page_redirects(self):
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.url)


class UserManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("acc_admin", password="x")
        self.client.force_login(self.admin)

    def test_user_list_renders(self):
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 200)

    def test_create_user_with_role(self):
        r = self.client.post(reverse("user_create"), {
            "username": "newasst", "first_name": "New", "last_name": "Assistant",
            "email": "n@example.com", "role": ASSISTANT,
            "password1": "Str0ngPass!23", "password2": "Str0ngPass!23"})
        self.assertIn(r.status_code, (200, 302))
        u = User.objects.filter(username="newasst").first()
        self.assertIsNotNone(u)
        self.assertTrue(u.groups.filter(name=ASSISTANT).exists())


class UserManagementAccessTests(TestCase):
    def test_non_admin_cannot_list_users(self):
        u = _user("plain_as", ASSISTANT)
        self.client.force_login(u)
        r = self.client.get(reverse("user_list"))
        self.assertIn(r.status_code, (302, 403))


class TwoFactorTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.u = User.objects.create_user("tfa", password="pw12345678")

    def _enrol(self, client):
        import pyotp
        from accounts.models import TwoFactor
        client.force_login(self.u)
        client.get("/2fa/setup/")
        tf = TwoFactor.objects.get(user=self.u)
        client.post("/2fa/setup/", {"token": pyotp.TOTP(tf.secret).now()})
        tf.refresh_from_db()
        return tf

    def test_enrolment_confirms_and_issues_codes(self):
        from django.test import Client
        tf = self._enrol(Client())
        self.assertTrue(tf.confirmed)
        self.assertEqual(len(tf.get_recovery_codes()), 10)

    def test_login_requires_totp_when_enrolled(self):
        import pyotp
        from django.test import Client
        from accounts.models import TwoFactor
        self._enrol(Client())
        tf = TwoFactor.objects.get(user=self.u)
        c = Client()
        r = c.post("/accounts/login/", {"username": "tfa", "password": "pw12345678"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/2fa/verify", r.url)        # not logged in yet
        # wrong code stays on the gate
        bad = c.post("/2fa/verify/", {"token": "000000"})
        self.assertEqual(bad.status_code, 200)
        # correct code completes login
        good = c.post("/2fa/verify/", {"token": pyotp.TOTP(tf.secret).now()})
        self.assertEqual(good.status_code, 302)

    def test_recovery_code_single_use(self):
        from django.test import Client
        from accounts.models import TwoFactor
        self._enrol(Client())
        tf = TwoFactor.objects.get(user=self.u)
        code = tf.get_recovery_codes()[0]
        c = Client()
        c.post("/accounts/login/", {"username": "tfa", "password": "pw12345678"})
        r = c.post("/2fa/verify/", {"token": code, "recovery": "1"})
        self.assertEqual(r.status_code, 302)
        tf.refresh_from_db()
        self.assertNotIn(code, tf.get_recovery_codes())   # consumed
        self.assertEqual(len(tf.get_recovery_codes()), 9)

    def test_no_2fa_means_normal_login(self):
        from django.test import Client
        c = Client()
        r = c.post("/accounts/login/", {"username": "tfa", "password": "pw12345678"})
        # no 2FA enrolled → straight in (redirect to next/dashboard, not the gate)
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/2fa/verify", r.url)
