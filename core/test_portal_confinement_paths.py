"""The paths ``PortalConfinementMiddleware`` lets a confined member keep.

The middleware confines a portal-only login to /portal/ and a short list of
prefixes described as "authentication itself or a page about their own login".
Two of those prefixes — ``/accounts/password_reset`` and ``/accounts/reset`` —
matched nothing. They look like Django's stock ``PasswordResetView`` routes, but
this application never mounted those: the self-service flow lives at
``/accounts/forgot-password/`` and its children (config/urls.py), and the only
``reset`` route in accounts/urls.py is the *administrator-triggered*
``/users/<pk>/reset-password/``, a different feature belonging to a different
role. So the one group of users least able to phone the office for help — the
congregation, on the member portal — was bounced back to the portal home page
the moment they clicked "forgot my password".

Prefix strings drift away from URLconfs silently, which is how this happened, so
these tests deliberately do not hardcode any path. They ``reverse()`` the URL
names and ask the middleware about the result: move or rename a route and the
test moves with it, but drop it out of the allowlist and the test fails.
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.middleware import PortalConfinementMiddleware
from core.roles import MEMBER, is_portal_only


def _allows(url):
    return PortalConfinementMiddleware(lambda r: None)._allowed(url)


class ConfinedMemberKeepsTheirOwnLoginPagesTests(TestCase):
    """Every route a member needs in order to get back into their own account
    must survive the confinement, resolved by name rather than by string."""

    def test_the_self_service_password_reset_flow_is_allowed(self):
        for name in ("self_reset_request", "self_reset_verify"):
            url = reverse(name)
            self.assertTrue(
                _allows(url),
                f"{name} resolves to {url}, which the confinement middleware "
                f"does not allow — a member who forgot their password is sent "
                f"back to the portal instead of the reset form")

    def test_the_emailed_reset_confirm_link_is_allowed(self):
        """The email channel's second half takes a uidb64 and a token in the
        path, so it is reversed with placeholder arguments — the prefix is what
        matters, not the particular token."""
        url = reverse("password_reset_confirm",
                      kwargs={"uidb64": "MQ", "token": "set-password"})
        self.assertTrue(_allows(url),
                        f"the emailed reset link ({url}) is not allowed through")

    def test_sign_in_sign_out_and_password_change_are_allowed(self):
        for name in ("login", "logout", "password_change"):
            url = reverse(name)
            self.assertTrue(_allows(url), f"{name} ({url}) is not allowed through")

    def test_the_portal_itself_is_allowed(self):
        self.assertTrue(_allows(reverse("portal_home")))

    def test_office_pages_are_still_confined(self):
        """The other direction: widening the allowlist must not have opened the
        office application. A prefix like '/accounts/' rather than the specific
        pages would quietly hand a member the whole user-administration module."""
        for name in ("dashboard", "settings", "user_list"):
            try:
                url = reverse(name)
            except Exception:
                continue
            self.assertFalse(
                _allows(url),
                f"{name} ({url}) is reachable by a confined portal member")


class ConfinedMemberReachesTheResetFormTests(TestCase):
    """End to end through the real middleware stack, because the unit test above
    only proves the prefix table agrees with itself."""

    def setUp(self):
        self.user = User.objects.create_user("portalonlymember",
                                             password="portal-pass-123")
        self.user.groups.add(Group.objects.get_or_create(name=MEMBER)[0])
        self.client = Client()
        self.client.login(username="portalonlymember", password="portal-pass-123")

    def test_the_fixture_really_is_a_confined_member(self):
        """Guards the test itself: if this login stopped being portal-only the
        assertions below would pass for the wrong reason."""
        self.assertTrue(is_portal_only(self.user))

    def test_a_signed_in_member_can_open_the_forgot_password_form(self):
        response = self.client.get(reverse("self_reset_request"))
        self.assertEqual(
            response.status_code, 200,
            f"a portal member asking to reset their password was sent to "
            f"{response.headers.get('Location', '(nowhere)')!r}")

    def test_a_signed_in_member_is_still_bounced_off_the_dashboard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/portal/"))
