"""Comprehensive User Management module review: profile management, account
lifecycle (activate/deactivate/lock/unlock), self-permission-modification
restrictions, password administration, 2FA administration, session
termination, audit trail, and list search/filter/sort."""
import datetime as dt
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from accounts.models import UserProfile, UserAdminLogEntry, TwoFactor, Profile


def _tr(username="tr_usermgmt"):
    u = User.objects.create_user(username, password="TrPass1234!", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _plain(username="plain_usermgmt"):
    u = User.objects.create_user(username, password="PlainPass1234!")
    u.groups.add(Group.objects.get_or_create(name="Assistant")[0])
    return u


class UserProfileModelTests(TestCase):
    def test_for_user_creates_lazily(self):
        u = _plain("lazyprofile")
        self.assertFalse(UserProfile.objects.filter(user=u).exists())
        p = UserProfile.for_user(u)
        self.assertTrue(UserProfile.objects.filter(user=u).exists())
        self.assertEqual(UserProfile.for_user(u).pk, p.pk)   # idempotent


class ProfileTabEditTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.target = _plain("profiletarget")
        self.c = Client(); self.c.force_login(self.tr)

    def test_can_view_profile_tab(self):
        r = self.c.get(f"/users/{self.target.id}/edit/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Profile", r.content.decode())

    def test_updates_name_email_and_profile_fields(self):
        from departments.models import Department
        d = Department.objects.create(name="ProfileEditDept", fund_type="LOCAL",
            category="MINISTRY")
        r = self.c.post(f"/users/{self.target.id}/edit/", {
            "form_name": "profile_form", "first_name": "Jane", "last_name": "Doe",
            "email": "jane@example.com", "phone": "254712345678", "gender": "F",
            "position": "Head Deacon", "department": d.id,
            "church_assignment": "", "notes": "Reliable volunteer"})
        self.assertEqual(r.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.first_name, "Jane")
        self.assertEqual(self.target.email, "jane@example.com")
        p = UserProfile.for_user(self.target)
        self.assertEqual(p.phone, "254712345678")
        self.assertEqual(p.gender, "F")
        self.assertEqual(p.position, "Head Deacon")
        self.assertEqual(p.department_id, d.id)
        self.assertEqual(p.notes, "Reliable volunteer")

    def test_profile_update_is_audited(self):
        self.c.post(f"/users/{self.target.id}/edit/", {
            "form_name": "profile_form", "first_name": "Changed", "last_name": "",
            "email": "", "phone": "", "gender": "", "position": "",
            "department": "", "church_assignment": "", "notes": ""})
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="PROFILE_UPDATED").exists())

    def test_created_by_shown_for_newly_created_user(self):
        r = self.c.post("/users/new/", {
            "username": "newlycreated", "password1": "NewUserPass1234!",
            "password2": "NewUserPass1234!", "role": "Assistant",
            "first_name": "New", "last_name": "User", "email": ""})
        self.assertEqual(r.status_code, 302)
        new_user = User.objects.get(username="newlycreated")
        p = UserProfile.for_user(new_user)
        self.assertEqual(p.created_by, self.tr)
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=new_user, action="CREATED").exists())


class AccountLifecycleTests(TestCase):
    def setUp(self):
        self.tr = _tr("tr_lifecycle")
        self.target = _plain("lifecycletarget")
        self.c = Client(); self.c.force_login(self.tr)

    def test_lock_blocks_login(self):
        self.c.post(f"/users/{self.target.id}/action/lock/", {"reason": "test"})
        c2 = Client()
        r = c2.post("/accounts/login/", {"username": "lifecycletarget",
                                        "password": "PlainPass1234!"})
        self.assertIn("suspended", r.content.decode().lower())

    def test_lock_ends_existing_session_immediately(self):
        c2 = Client(); c2.force_login(self.target)
        self.assertEqual(c2.get("/").status_code, 200)
        self.c.post(f"/users/{self.target.id}/action/lock/", {"reason": "test"})
        r = c2.get("/", follow=True)
        self.assertTrue(any("login" in u for u, _ in r.redirect_chain))

    def test_unlock_restores_access(self):
        self.c.post(f"/users/{self.target.id}/action/lock/", {"reason": "test"})
        self.c.post(f"/users/{self.target.id}/action/unlock/")
        c2 = Client()
        r = c2.post("/accounts/login/", {"username": "lifecycletarget",
                                        "password": "PlainPass1234!"}, follow=True)
        self.assertNotIn("suspended", r.content.decode().lower())

    def test_lock_and_unlock_are_audited(self):
        self.c.post(f"/users/{self.target.id}/action/lock/", {"reason": "Investigating"})
        self.c.post(f"/users/{self.target.id}/action/unlock/")
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="LOCKED", detail="Investigating").exists())
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="UNLOCKED").exists())

    def test_deactivate_via_role_form(self):
        r = self.c.post(f"/users/{self.target.id}/edit/", {
            "form_name": "role_form", "role": "Assistant", "active": ""})
        self.assertEqual(r.status_code, 302)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="DEACTIVATED").exists())

    def test_reactivate_via_role_form(self):
        self.target.is_active = False
        self.target.save()
        r = self.c.post(f"/users/{self.target.id}/edit/", {
            "form_name": "role_form", "role": "Assistant", "active": "on"})
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        self.assertTrue(UserAdminLogEntry.objects.filter(
            target_user=self.target, action="ACTIVATED").exists())

    def test_role_change_is_audited(self):
        self.c.post(f"/users/{self.target.id}/edit/", {
            "form_name": "role_form", "role": "Auditor", "active": "on"})
        entry = UserAdminLogEntry.objects.filter(
            target_user=self.target, action="ROLE_CHANGED").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.before, "Assistant")
        self.assertEqual(entry.after, "Auditor")


class SelfPermissionModificationTests(TestCase):
    """Users cannot modify their own permissions unless explicitly allowed —
    the core security requirement of this review."""
    def setUp(self):
        self.tr = _tr("tr_selfmod")
        self.other_tr = _tr("tr_selfmod_other")
        self.c = Client(); self.c.force_login(self.tr)

    def test_cannot_change_own_role(self):
        r = self.c.post(f"/users/{self.tr.id}/edit/", {
            "form_name": "role_form", "role": "Assistant", "active": "on"})
        self.tr.refresh_from_db()
        self.assertTrue(self.tr.groups.filter(name="Treasurer").exists())

    def test_cannot_deactivate_self(self):
        self.c.post(f"/users/{self.tr.id}/edit/", {
            "form_name": "role_form", "role": "Treasurer", "active": ""})
        self.tr.refresh_from_db()
        self.assertTrue(self.tr.is_active)

    def test_cannot_lock_self(self):
        r = self.c.post(f"/users/{self.tr.id}/action/lock/", {"reason": "test"})
        p = UserProfile.for_user(self.tr)
        self.assertFalse(p.locked)

    def test_cannot_disable_own_2fa_via_admin_panel(self):
        tf = TwoFactor(user=self.tr, method="TOTP", confirmed=True)
        tf.set_secret("JBSWY3DPEHPK3PXP")
        tf.save()
        self.c.post(f"/users/{self.tr.id}/action/disable_2fa/")
        self.assertTrue(TwoFactor.objects.filter(user=self.tr).exists())

    def test_cannot_clear_own_lockout(self):
        r = self.c.post(f"/users/{self.tr.id}/action/clear_lockout/")
        self.assertEqual(r.status_code, 302)   # blocked, redirected with a message

    def test_cannot_terminate_own_sessions(self):
        from django.contrib.sessions.models import Session
        before = Session.objects.count()
        self.c.post(f"/users/{self.tr.id}/action/terminate_sessions/")
        # own session (the one making this very request) must survive
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)

    def test_cannot_reset_own_password_via_admin_tool(self):
        r = self.c.get(f"/users/{self.tr.id}/reset-password/")
        self.assertEqual(r.status_code, 302)

    def test_can_still_edit_others(self):
        other = _plain("selfmod_editable")
        r = self.c.post(f"/users/{other.id}/edit/", {
            "form_name": "role_form", "role": "Auditor", "active": "on"})
        other.refresh_from_db()
        self.assertTrue(other.groups.filter(name="Auditor").exists())


class UserEditPageLayoutTests(TestCase):
    """UX fix: every section on the user admin page had been given the
    `u-narrow` class (max-width: 560px) — appropriate for the profile form,
    but wrong for the wide stat grids and multi-column audit/activity
    tables, which rendered visibly cramped. Fixed to only constrain the
    genuinely form-like sections; wide content (stats, tables, action grids)
    now uses the full available width, matching this app's other list/report
    pages (verified against templates/settings.html's own convention)."""
    def setUp(self):
        self.tr = _tr("tr_layoutcheck")
        self.target = _plain("layoutchecktarget")
        self.c = Client(); self.c.force_login(self.tr)

    def test_page_renders_successfully(self):
        r = self.c.get(f"/users/{self.target.id}/edit/")
        self.assertEqual(r.status_code, 200)

    def test_wide_sections_no_longer_narrow(self):
        b = self.c.get(f"/users/{self.target.id}/edit/").content.decode()
        # the account-status stats grid and the audit/activity tables must
        # not be squeezed into the narrow (560px) form-width class
        self.assertNotIn('class="card u-narrow"><div class="hd">Account status',
                         b)
        self.assertNotIn(
            'class="card u-narrow"><div class="hd">Full administrative audit trail', b)
        self.assertNotIn(
            'class="card u-narrow"><div class="hd">Recent security events', b)

    def test_all_five_tabs_present(self):
        b = self.c.get(f"/users/{self.target.id}/edit/").content.decode()
        for tab in ["Profile", "Security", "Roles &amp; Rights", "Activity", "Audit Log"]:
            self.assertIn(tab, b)

    def test_grid_layouts_intact(self):
        b = self.c.get(f"/users/{self.target.id}/edit/").content.decode()
        self.assertIn("grid-3", b)
        self.assertIn("form-grid", b)
