"""Admin password reset, forced password change, 2FA administration, failed-
login lockout clearing, session termination, and account cloning."""
import datetime as dt
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from accounts.models import UserProfile, UserAdminLogEntry, TwoFactor


def _tr(username="tr_adminactions"):
    u = User.objects.create_user(username, password="TrPass1234!", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _plain(username="plain_adminactions"):
    u = User.objects.create_user(username, password="PlainPass1234!")
    u.groups.add(Group.objects.get_or_create(name="Assistant")[0])
    return u


class AdminPasswordResetTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.target = _plain()
        self.c = Client(); self.c.force_login(self.tr)

    def test_reset_sets_new_password(self):
        r = self.c.post(f"/users/{self.target.id}/reset-password/", {
            "new_password": "BrandNewSecure99!", "force_change": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("BrandNewSecure99!", r.content.decode())
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("BrandNewSecure99!"))

    def test_reset_with_force_change_sets_flag(self):
        self.c.post(f"/users/{self.target.id}/reset-password/", {
            "new_password": "BrandNewSecure99!", "force_change": "on"})
        p = UserProfile.for_user(self.target)
        self.assertTrue(p.must_change_password)

    def test_reset_without_force_change_does_not_set_flag(self):
        self.c.post(f"/users/{self.target.id}/reset-password/", {
            "new_password": "BrandNewSecure99!"})
        p = UserProfile.for_user(self.target)
        self.assertFalse(p.must_change_password)

    def test_reset_is_audited(self):
        self.c.post(f"/users/{self.target.id}/reset-password/", {
            "new_password": "BrandNewSecure99!", "force_change": "on"})
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="PASSWORD_RESET").exists())

    def test_weak_password_rejected(self):
        r = self.c.post(f"/users/{self.target.id}/reset-password/", {
            "new_password": "12345"})
        self.assertEqual(r.status_code, 200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.check_password("12345"))

    def test_forced_password_change_actually_blocks_navigation(self):
        self.c.post(f"/users/{self.target.id}/reset-password/", {
            "new_password": "BrandNewSecure99!", "force_change": "on"})
        # the reset itself invalidates any prior session (Django's own
        # session-auth-hash security feature); the realistic check is that a
        # FRESH login with the new password still gets routed to change it
        c2 = Client()
        c2.post("/accounts/login/", {"username": self.target.username,
                                     "password": "BrandNewSecure99!"})
        r = c2.get("/", follow=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("password_change", r.url)


class TwoFactorAdminTests(TestCase):
    def setUp(self):
        self.tr = _tr("tr_2faadmin")
        self.target = _plain("target_2faadmin")
        tf = TwoFactor(user=self.target, method="TOTP", confirmed=True)
        tf.set_secret("JBSWY3DPEHPK3PXP")
        tf.save()
        self.c = Client(); self.c.force_login(self.tr)

    def test_disable_removes_two_factor_record(self):
        self.c.post(f"/users/{self.target.id}/action/disable_2fa/")
        self.assertFalse(TwoFactor.objects.filter(user=self.target).exists())

    def test_disable_is_audited(self):
        self.c.post(f"/users/{self.target.id}/action/disable_2fa/")
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="TWO_FA_DISABLED").exists())

    def test_security_tab_shows_2fa_status(self):
        b = self.c.get(f"/users/{self.target.id}/edit/?tab=security").content.decode()
        self.assertIn("Enabled", b)


class LockoutClearTests(TestCase):
    def setUp(self):
        self.tr = _tr("tr_lockoutclear")
        self.target = _plain("target_lockoutclear")
        self.c = Client(); self.c.force_login(self.tr)

    def test_clear_lockout_resets_axes_attempts(self):
        from axes.models import AccessAttempt
        AccessAttempt.objects.create(username=self.target.username,
            ip_address="10.0.0.5", failures_since_start=5)
        self.c.post(f"/users/{self.target.id}/action/clear_lockout/")
        self.assertFalse(AccessAttempt.objects.filter(
            username=self.target.username).exists())

    def test_clear_lockout_is_audited(self):
        self.c.post(f"/users/{self.target.id}/action/clear_lockout/")
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="LOGIN_LOCKOUT_CLEARED").exists())


class SessionTerminationTests(TestCase):
    def setUp(self):
        self.tr = _tr("tr_sessionterm")
        self.target = _plain("target_sessionterm")
        self.c = Client(); self.c.force_login(self.tr)

    def test_terminate_kills_active_session(self):
        c2 = Client(); c2.force_login(self.target)
        self.assertEqual(c2.get("/").status_code, 200)
        self.c.post(f"/users/{self.target.id}/action/terminate_sessions/")
        r = c2.get("/", follow=True)
        self.assertTrue(any("login" in u for u, _ in r.redirect_chain))

    def test_terminate_is_audited(self):
        c2 = Client(); c2.force_login(self.target)
        c2.get("/")
        self.c.post(f"/users/{self.target.id}/action/terminate_sessions/")
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="SESSIONS_TERMINATED").exists())

    def test_session_count_shown_on_security_tab(self):
        c2 = Client(); c2.force_login(self.target)
        c2.get("/")
        b = self.c.get(f"/users/{self.target.id}/edit/?tab=security").content.decode()
        self.assertIn("Active sessions", b)


class UserCloneTests(TestCase):
    def setUp(self):
        self.tr = _tr("tr_clone")
        self.source = _plain("clonesource")
        self.c = Client(); self.c.force_login(self.tr)

    def test_clone_copies_role_not_credentials(self):
        r = self.c.post(f"/users/{self.source.id}/clone/", {
            "username": "clonedaccount", "first_name": "Cloned", "last_name": "User",
            "email": ""})
        self.assertEqual(r.status_code, 302)
        new_user = User.objects.get(username="clonedaccount")
        self.assertEqual(set(new_user.groups.all()), set(self.source.groups.all()))
        self.assertFalse(new_user.check_password("PlainPass1234!"))   # not source's password

    def test_clone_forces_password_change(self):
        self.c.post(f"/users/{self.source.id}/clone/", {
            "username": "clonedaccount2", "first_name": "", "last_name": "", "email": ""})
        new_user = User.objects.get(username="clonedaccount2")
        p = UserProfile.for_user(new_user)
        self.assertTrue(p.must_change_password)

    def test_clone_is_audited(self):
        self.c.post(f"/users/{self.source.id}/clone/", {
            "username": "clonedaccount3", "first_name": "", "last_name": "", "email": ""})
        new_user = User.objects.get(username="clonedaccount3")
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=new_user, action="CLONED").exists())

    def test_clone_rejects_duplicate_username(self):
        r = self.c.post(f"/users/{self.source.id}/clone/", {
            "username": self.source.username, "first_name": "", "last_name": "", "email": ""})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(User.objects.filter(username=self.source.username).count(), 1)
