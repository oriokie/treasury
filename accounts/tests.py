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
