"""Critical fix: AXES_LOCKOUT_PARAMETERS was a flat list (["username",
"ip_address"]), which django-axes treats as "locked out if EITHER the
username OR the ip_address alone crosses the failure limit" - independently.
Several people sharing one office network/IP would all get locked out the
moment any ONE of them mistyped their password enough times, since the IP
itself trips the limit regardless of which username was being tried.

Fixed to the nested/combination form ([["username", "ip_address"]]), which
locks out only the specific (username, ip) pair that actually failed
repeatedly. Also fixed the lockout response itself, which previously showed
django-axes' own bare, unstyled page - now redirects back to the app's own
login page with a clear, styled message.

axes is disabled during the test suite by this app's own settings
(AXES_ENABLED = "test" not in sys.argv) to avoid interfering with other
tests' rapid login attempts, so every test here explicitly re-enables it."""
from django.contrib.auth.models import User, Group
from django.test import TestCase, Client, override_settings
from axes.models import AccessAttempt


def _user(username, password):
    u = User.objects.create_user(username, password=password)
    u.groups.add(Group.objects.get_or_create(name="Assistant")[0])
    return u


@override_settings(AXES_ENABLED=True)
class AxesLockoutScopeTests(TestCase):
    """Confirms the actual reported bug is fixed: one user's failed attempts
    must not lock out a different user sharing the same IP address."""
    def setUp(self):
        AccessAttempt.objects.all().delete()

    def test_one_users_failures_do_not_lock_out_a_different_user_same_ip(self):
        u1 = _user("axes_scope_user1", "CorrectPass111!")
        u2 = _user("axes_scope_user2", "CorrectPass222!")
        c = Client()   # the SAME client/session - i.e. the same source IP
        for _ in range(6):
            c.post("/accounts/login/", {"username": "axes_scope_user1",
                                        "password": "WrongPassword!"})
        # user1 should now be locked out from this IP
        r1 = c.post("/accounts/login/", {"username": "axes_scope_user1",
                                        "password": "CorrectPass111!"}, follow=True)
        self.assertIn("axeslocked", str(r1.redirect_chain) + r1.request.get("QUERY_STRING", ""))
        # user2, from the exact same client/IP, must still be able to log in
        c2 = Client()
        r2 = c2.post("/accounts/login/", {"username": "axes_scope_user2",
                                         "password": "CorrectPass222!"})
        self.assertEqual(r2.status_code, 302)
        self.assertNotIn("axeslocked", r2.url)

    def test_the_failing_user_is_genuinely_locked_from_that_ip(self):
        _user("axes_scope_user3", "CorrectPass333!")
        c = Client()
        for _ in range(6):
            c.post("/accounts/login/", {"username": "axes_scope_user3",
                                        "password": "WrongPassword!"})
        r = c.post("/accounts/login/", {"username": "axes_scope_user3",
                                       "password": "CorrectPass333!"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("axeslocked", r.url)

    def test_lockout_config_is_the_combination_form_not_flat(self):
        from django.conf import settings
        self.assertEqual(settings.AXES_LOCKOUT_PARAMETERS, [["username", "ip_address"]])


@override_settings(AXES_ENABLED=True)
class AxesLockoutDisplayTests(TestCase):
    """The lockout response itself: redirects to the app's own login page
    with a clear message, instead of axes' bare default response."""
    def setUp(self):
        AccessAttempt.objects.all().delete()

    def test_lockout_redirects_to_login_page_not_a_bare_axes_page(self):
        _user("axes_display_user1", "CorrectPass111!")
        c = Client()
        for _ in range(6):
            r = c.post("/accounts/login/", {"username": "axes_display_user1",
                                            "password": "WrongPassword!"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, "/accounts/login/?axeslocked=1")

    def test_the_message_renders_within_the_apps_own_login_template(self):
        _user("axes_display_user2", "CorrectPass222!")
        c = Client()
        for _ in range(6):
            c.post("/accounts/login/", {"username": "axes_display_user2",
                                        "password": "WrongPassword!"})
        r = c.get("/accounts/login/?axeslocked=1")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Too many failed sign-in attempts", b)
        # confirms it's rendered inside the app's own page chrome, not a
        # bare/separate response - the login form itself must still be present
        self.assertIn('name="username"', b)
        self.assertIn("Welcome back", b)
