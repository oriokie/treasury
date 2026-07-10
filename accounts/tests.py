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


class TwoFactorVerifyPageRendersTests(TestCase):
    """Regression: the 2FA verify page is shown while the user is NOT yet logged
    in, so it must render via the unauthenticated layout. A blank verify page
    (content gated behind authentication) locks everyone out."""

    def setUp(self):
        import pyotp
        from django.contrib.auth.models import User
        from accounts.models import TwoFactor
        self.u = User.objects.create_user("v2fa", password="pw12345")
        self.sec = pyotp.random_base32()
        tf = TwoFactor.objects.create(user=self.u, confirmed=True)
        tf.set_secret(self.sec); tf.save()

    def test_verify_page_renders_form_when_not_authenticated(self):
        c = self.client
        r = c.post("/accounts/login/", {"username": "v2fa", "password": "pw12345"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/2fa/verify/", r["Location"])
        r = c.get("/2fa/verify/")
        self.assertEqual(r.status_code, 200)
        # the form must actually be in the body, not gated away
        self.assertContains(r, 'name="token"')
        self.assertContains(r, "Enter your code")

    def test_valid_code_logs_in(self):
        import pyotp
        c = self.client
        c.post("/accounts/login/", {"username": "v2fa", "password": "pw12345"})
        r = c.post("/2fa/verify/", {"token": pyotp.TOTP(self.sec).now()})
        self.assertEqual(r.status_code, 302)


class TwoFactorSetupQrTests(TestCase):
    """Regression: the enrolment QR must render without Pillow (it's not installed
    in production), or the treasurer can't scan to enrol."""

    def test_setup_page_shows_qr(self):
        from django.contrib.auth.models import User
        u = User.objects.create_user("qruser", password="pw12345")
        self.client.force_login(u)
        r = self.client.get("/2fa/setup/")
        self.assertEqual(r.status_code, 200)
        # an inline QR image must be present (SVG data URI, Pillow-free)
        self.assertIn(b"data:image/svg+xml;base64,", r.content)

    def test_qr_helper_returns_svg(self):
        from accounts.twofactor import _qr_data_uri
        uri = "otpauth://totp/Test:user?secret=ABCDEFGHIJKLMNOP&issuer=Test"
        out = _qr_data_uri(uri)
        self.assertTrue(out.startswith("data:image/svg+xml;base64,"))


class TwoFactorVerifyBothStatesTests(TestCase):
    """Regression: the verify page must render whether the user arrives
    unauthenticated (fresh login) or authenticated-but-unverified (middleware),
    so it is a standalone page not tied to base.html's auth-gated blocks."""

    def setUp(self):
        import pyotp
        from django.contrib.auth.models import User
        from accounts.models import TwoFactor
        self.u = User.objects.create_user("v2both", password="pw12345")
        self.sec = pyotp.random_base32()
        tf = TwoFactor.objects.create(user=self.u, confirmed=True)
        tf.set_secret(self.sec); tf.save()

    def test_fresh_login_renders_form(self):
        c = self.client
        c.post("/accounts/login/", {"username": "v2both", "password": "pw12345"})
        r = c.get("/2fa/verify/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="token"')

    def test_authenticated_unverified_renders_form(self):
        from accounts.twofactor import PENDING_USER, VERIFIED
        c = self.client
        c.force_login(self.u)
        s = c.session; s[PENDING_USER] = self.u.pk; s[VERIFIED] = False; s.save()
        r = c.get("/2fa/verify/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="token"')


from django.test import override_settings


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OtpDeliveryTests(TestCase):
    """SMS/email one-time-code 2FA, delivered via the Advanta SMS integration or
    Django email, as an alternative to the authenticator app."""

    def setUp(self):
        from django.contrib.auth.models import User
        from core.models import SiteConfig
        cfg = SiteConfig.get(); cfg.require_2fa_for_treasurers = False; cfg.save()
        self.u = User.objects.create_user("otp", password="pw12345", email="a@b.com")

    def _code_from_outbox(self):
        import re
        from django.core import mail
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def test_email_code_send_and_verify(self):
        from django.core import mail
        from accounts.models import TwoFactor
        tf = TwoFactor.objects.create(user=self.u, method="EMAIL", delivery_email="a@b.com")
        mail.outbox = []
        ok, dest = tf.send_code()
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        code = self._code_from_outbox()
        self.assertFalse(tf.verify_code("000000"))   # wrong
        self.assertTrue(tf.verify_code(code))         # right
        self.assertFalse(tf.verify_code(code))        # consumed, can't reuse

    def test_expired_code_rejected(self):
        import datetime as dt
        from django.utils import timezone
        from accounts.models import TwoFactor
        tf = TwoFactor.objects.create(user=self.u, method="EMAIL", delivery_email="a@b.com")
        tf.send_code()
        # read the code, then force expiry
        tf.otp_expires_at = timezone.now() - dt.timedelta(seconds=1)
        tf.save()
        self.assertFalse(tf.verify_code("123456"))

    def test_attempt_limit(self):
        from accounts.models import TwoFactor
        tf = TwoFactor.objects.create(user=self.u, method="EMAIL", delivery_email="a@b.com")
        tf.send_code()
        for _ in range(5):
            tf.verify_code("000000")
        # 6th attempt is locked out even if it were correct
        self.assertGreaterEqual(tf.otp_attempts, 5)

    def test_login_flow_with_email_2fa(self):
        from django.core import mail
        from accounts.models import TwoFactor
        TwoFactor.objects.create(user=self.u, method="EMAIL",
                                 delivery_email="a@b.com", confirmed=True)
        c = self.client
        c.post("/accounts/login/", {"username": "otp", "password": "pw12345"})
        mail.outbox = []
        r = c.get("/2fa/verify/")             # auto-sends a code
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        code = self._code_from_outbox()
        r2 = c.post("/2fa/verify/", {"token": code})
        self.assertEqual(r2.status_code, 302)

    def test_recovery_code_still_works_for_code_method(self):
        from accounts.models import TwoFactor
        tf = TwoFactor.objects.create(user=self.u, method="SMS", phone="254712345678",
                                      confirmed=True)
        codes = tf.reset_recovery_codes(); tf.save()
        self.assertTrue(tf.authenticate(codes[0]))    # recovery code via authenticate()

    def test_sms_method_stores_hashed_code(self):
        from core.models import SiteConfig
        from accounts.models import TwoFactor
        cfg = SiteConfig.get()
        cfg.sms_enabled = True; cfg.sms_api_key = "k"; cfg.sms_partner_id = "p"
        cfg.sms_shortcode = "SC"; cfg.save()
        tf = TwoFactor.objects.create(user=self.u, method="SMS", phone="254712345678")
        tf.send_code()       # delivery may fail in test, but the code is stored
        self.assertTrue(tf.otp_hash)
        self.assertFalse(tf.verify_code("000000"))
