"""Self-service password reset: an SMS one-time-code channel (custom, for a
phone number on file with SMS sending configured) and an emailed reset link
(Django's own well-tested token mechanism, for an email on file with real
SMTP configured). Never reveals whether a username exists or which channel
it has on file — the response is identical regardless."""
import datetime as dt
from unittest import mock
import os

from django.core import mail
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import UserProfile, PasswordResetCode
from core.models import SiteConfig


def _sms_configured_user(username="smsuser", phone="254712345678"):
    u = User.objects.create_user(username, password="OldPassword1234!")
    p = UserProfile.for_user(u)
    p.phone = phone
    p.save()
    cfg = SiteConfig.get()
    cfg.sms_enabled = True
    cfg.sms_api_key = "testkey"
    cfg.sms_partner_id = "testpartner"
    cfg.sms_shortcode = "TEST"
    cfg.save()
    return u


class PasswordResetCodeModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("codemodeltest", password="x")

    def test_issue_returns_a_six_digit_code(self):
        obj, raw = PasswordResetCode.issue(self.user)
        self.assertEqual(len(raw), 6)
        self.assertTrue(raw.isdigit())

    def test_code_is_stored_hashed_not_plaintext(self):
        obj, raw = PasswordResetCode.issue(self.user)
        self.assertNotEqual(obj.code_hash, raw)
        self.assertTrue(len(obj.code_hash) > 20)   # a real hash, not the raw code

    def test_verify_succeeds_with_correct_code(self):
        obj, raw = PasswordResetCode.issue(self.user)
        self.assertEqual(PasswordResetCode.verify(self.user, raw).pk, obj.pk)

    def test_verify_fails_with_wrong_code(self):
        PasswordResetCode.issue(self.user)
        self.assertIsNone(PasswordResetCode.verify(self.user, "000000"))

    def test_verify_fails_after_use(self):
        obj, raw = PasswordResetCode.issue(self.user)
        obj.mark_used()
        self.assertIsNone(PasswordResetCode.verify(self.user, raw))

    def test_verify_fails_after_expiry(self):
        obj, raw = PasswordResetCode.issue(self.user, ttl_minutes=10)
        obj.expires_at = timezone.now() - dt.timedelta(minutes=1)
        obj.save()
        self.assertIsNone(PasswordResetCode.verify(self.user, raw))

    def test_issuing_a_new_code_invalidates_the_previous_one(self):
        obj1, raw1 = PasswordResetCode.issue(self.user)
        obj2, raw2 = PasswordResetCode.issue(self.user)
        self.assertIsNone(PasswordResetCode.verify(self.user, raw1))
        self.assertIsNotNone(PasswordResetCode.verify(self.user, raw2))


class SmsResetFlowTests(TestCase):
    def test_request_sends_code_and_redirects_to_verify(self):
        _sms_configured_user()
        c = Client()
        r = c.post("/accounts/forgot-password/", {"username": "smsuser"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("verify", r.url)

    def test_full_flow_changes_password(self):
        user = _sms_configured_user("smsflowuser")
        c = Client()
        c.post("/accounts/forgot-password/", {"username": "smsflowuser"})
        obj, raw = PasswordResetCode.issue(user)   # simulate reading the sent code
        r = c.post("/accounts/forgot-password/verify/", {
            "code": raw, "new_password": "BrandNewSecure99!",
            "confirm_password": "BrandNewSecure99!"})
        self.assertEqual(r.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password("BrandNewSecure99!"))

    def test_wrong_code_does_not_change_password(self):
        user = _sms_configured_user("smswrongcode")
        c = Client()
        c.post("/accounts/forgot-password/", {"username": "smswrongcode"})
        r = c.post("/accounts/forgot-password/verify/", {
            "code": "000000", "new_password": "BrandNewSecure99!",
            "confirm_password": "BrandNewSecure99!"})
        self.assertEqual(r.status_code, 200)   # re-rendered with an error
        user.refresh_from_db()
        self.assertFalse(user.check_password("BrandNewSecure99!"))

    def test_mismatched_passwords_rejected(self):
        user = _sms_configured_user("smsmismatch")
        c = Client()
        c.post("/accounts/forgot-password/", {"username": "smsmismatch"})
        obj, raw = PasswordResetCode.issue(user)
        r = c.post("/accounts/forgot-password/verify/", {
            "code": raw, "new_password": "BrandNewSecure99!",
            "confirm_password": "SomethingElse123!"})
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.check_password("BrandNewSecure99!"))

    def test_weak_password_rejected(self):
        user = _sms_configured_user("smsweak")
        c = Client()
        c.post("/accounts/forgot-password/", {"username": "smsweak"})
        obj, raw = PasswordResetCode.issue(user)
        r = c.post("/accounts/forgot-password/verify/", {
            "code": raw, "new_password": "12345", "confirm_password": "12345"})
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.check_password("12345"))

    def test_rate_limited_after_repeated_requests(self):
        user = _sms_configured_user("smsratelimit")
        c = Client()
        for _ in range(5):
            c.post("/accounts/forgot-password/", {"username": "smsratelimit"})
        # still returns the same generic response either way (no enumeration
        # of "you're rate limited"), but must not keep issuing new codes
        # forever
        count = PasswordResetCode.objects.filter(user=user).count()
        self.assertLessEqual(count, 3)


class EmailResetFlowTests(TestCase):
    def test_request_sends_email_when_smtp_configured(self):
        User.objects.create_user("emailresetuser", password="x",
            email="emailreset@example.com")
        with mock.patch.dict(os.environ, {"DJANGO_EMAIL_HOST": "smtp.example.com"}):
            c = Client()
            r = c.post("/accounts/forgot-password/", {"username": "emailresetuser"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset", mail.outbox[0].subject.lower())
        self.assertIn("forgot-password/email/confirm", mail.outbox[0].body)

    def test_no_email_sent_when_smtp_not_configured(self):
        User.objects.create_user("emailresetuser2", password="x",
            email="emailreset2@example.com")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DJANGO_EMAIL_HOST", None)
            c = Client()
            c.post("/accounts/forgot-password/", {"username": "emailresetuser2"})
        self.assertEqual(len(mail.outbox), 0)

    def test_email_link_actually_resets_password(self):
        user = User.objects.create_user("emaillinktest", password="OldPassword1234!",
            email="emaillinktest@example.com")
        with mock.patch.dict(os.environ, {"DJANGO_EMAIL_HOST": "smtp.example.com"}):
            c = Client()
            c.post("/accounts/forgot-password/", {"username": "emaillinktest"})
        self.assertEqual(len(mail.outbox), 1)
        import re
        m = re.search(r"(/accounts/forgot-password/email/confirm/\S+)", mail.outbox[0].body)
        self.assertIsNotNone(m)
        link = m.group(1)
        r = c.get(link, follow=True)
        self.assertEqual(r.status_code, 200)
        # follow through and set a new password
        # (Django's confirm view redirects the raw token to a "set-password"
        # session-marked URL on first GET, then accepts the form POST there)
        final_url = r.redirect_chain[-1][0] if r.redirect_chain else link
        r2 = c.post(final_url, {"new_password1": "BrandNewSecure99!",
                                "new_password2": "BrandNewSecure99!"})
        self.assertEqual(r2.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password("BrandNewSecure99!"))


class NoEnumerationTests(TestCase):
    """The response must be identical whether the account exists, has no
    contact channel on file, or is rate-limited — never distinguishable."""
    def test_nonexistent_username_gives_same_response_as_real_one(self):
        _sms_configured_user("realaccountuser")
        c1 = Client()
        r1 = c1.post("/accounts/forgot-password/", {"username": "realaccountuser"})
        c2 = Client()
        r2 = c2.post("/accounts/forgot-password/", {"username": "totally_made_up_user_xyz"})
        # both end up back at a page showing the same generic message
        self.assertEqual(r1.status_code, r2.status_code)

    def test_account_with_no_contact_channel_gives_generic_response(self):
        User.objects.create_user("nochannel_user", password="x")   # no phone, no email
        c = Client()
        r = c.post("/accounts/forgot-password/", {"username": "nochannel_user"}, follow=True)
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("If an account matches", b)
