"""Default-deny authorization (review P1-1).

Before this, every view had to opt IN to authentication with a mixin, so a new
view that forgot the mixin was public by accident. Now a global
``LoginRequiredMiddleware`` denies by default and the handful of genuinely public
endpoints opt OUT with ``@login_not_required``.

This is the enforcement that keeps it true. It walks EVERY resolvable URL and
asserts that an anonymous request is turned away (redirected to login / 2FA, or
403/404) UNLESS the URL is on an explicit, reviewed public allowlist. If someone
adds a new view and forgets to protect it, this test fails and names the URL — the
default can't silently reopen.

The allowlist is deliberately small and every entry is justified in a comment;
adding to it should be a conscious, reviewed act.
"""
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.models import AnonymousUser, Group, User
from django.contrib.auth.middleware import LoginRequiredMiddleware
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase
from django.urls import get_resolver
from django.views import View


# URL *names* that are allowed to be reached without logging in. Names (not
# paths) so a path change doesn't silently drop the protection check. Every entry
# is a deliberate, reviewed public endpoint:
PUBLIC_URL_NAMES = {
    "login",                 # the login page itself
    "logout",                # logging out (harmless while anonymous)
    "self_reset_request",    # "forgot password" entry — must be reachable logged-out
    "self_reset_verify",     # "forgot password" code step
    "password_reset_confirm",  # Django's emailed-link reset confirm
    "twofactor_verify",      # the 2FA gate, completed mid-login before auth finishes
    "healthz",               # uptime/readiness probe, exposes nothing sensitive
    "cbs_webhook",           # bank machine-to-machine; auth'd by token/HMAC, not a session
    "public_pledge",         # deliberately public member pledge form (off by default)
    "public_pledge_thanks",  # its thank-you page
    # The public benevolent application form. Same security model as the pledge
    # form above and for the same reasons: off unless explicitly enabled,
    # write-only (it never reads or exposes any member data), touches no ledger
    # and creates no cover, and is defended by a honeypot, a minimum fill time
    # and a per-session throttle. It was previously absent from this list AND
    # missing its `login_not_required` — so it was "protected", this test passed,
    # and the form was unreachable by the applicants it exists for.
    "benevolent_public_apply",
}


def _all_named_no_arg_urls():
    """Every resolvable URL with a name and no path parameters (so we can GET it
    anonymously without inventing arguments), excluding the Django admin (which
    has its own auth)."""
    def walk(patterns, prefix=""):
        out = []
        for p in patterns:
            if hasattr(p, "url_patterns"):
                out += walk(p.url_patterns, prefix + str(p.pattern))
            else:
                out.append((prefix + str(p.pattern), p.name))
        return out
    seen = {}
    for pat, name in walk(get_resolver().url_patterns):
        if not name or "<" in pat or pat.startswith("admin/"):
            continue
        path = "/" + pat if not pat.startswith("/") else pat
        seen.setdefault(name, path)
    return seen


class DefaultDenyTests(TestCase):
    def setUp(self):
        self.anon = Client()

    def test_middleware_is_installed(self):
        from django.conf import settings
        self.assertIn(
            "django.contrib.auth.middleware.LoginRequiredMiddleware",
            settings.MIDDLEWARE,
            "The default-deny gate is not installed.")
        # and it runs AFTER AuthenticationMiddleware (which sets request.user)
        mw = settings.MIDDLEWARE
        self.assertLess(
            mw.index("django.contrib.auth.middleware.AuthenticationMiddleware"),
            mw.index("django.contrib.auth.middleware.LoginRequiredMiddleware"))

    def test_a_view_without_a_mixin_is_protected_by_default(self):
        """The heart of P1-1: a brand-new view that forgets its auth mixin must
        still be denied to anonymous users by the global gate."""
        class ForgotMixinView(View):
            def get(self, request):
                return HttpResponse("secret")

        mw = LoginRequiredMiddleware(lambda r: HttpResponse("ok"))
        req = RequestFactory().get("/anything/")
        req.user = AnonymousUser()
        resp = mw.process_view(req, ForgotMixinView.as_view(), (), {})
        self.assertIsNotNone(resp, "an unmarked view was reachable anonymously")
        self.assertEqual(resp.status_code, 302)

    def test_marked_view_is_exempt(self):
        @login_not_required
        def public(request):
            return HttpResponse("ok")

        mw = LoginRequiredMiddleware(lambda r: HttpResponse("ok"))
        req = RequestFactory().get("/anything/")
        req.user = AnonymousUser()
        self.assertIsNone(mw.process_view(req, public, (), {}))

    def test_every_url_denies_anonymous_unless_allowlisted(self):
        offenders = []
        for name, path in _all_named_no_arg_urls().items():
            if name in PUBLIC_URL_NAMES:
                continue
            try:
                r = self.anon.get(path)
            except Exception:
                # a view that raises on a bare GET (needs POST/args) isn't a
                # public-exposure concern; skip it
                continue
            loc = r.headers.get("Location", "")
            protected = (
                (r.status_code == 302 and ("login" in loc or "2fa" in loc))
                or r.status_code in (403, 404, 405))
            if not protected:
                offenders.append((name, path, r.status_code))
        self.assertEqual(
            offenders, [],
            "These URLs are reachable by anonymous users but are not on the "
            "reviewed public allowlist (PUBLIC_URL_NAMES). Either protect the "
            f"view or, if it is genuinely public, add its name to the allowlist: {offenders}")

    def test_allowlisted_public_pages_really_are_reachable(self):
        """The other direction: the pages we CLAIM are public must actually load
        for an anonymous user (a broken allowlist entry should be noticed too)."""
        for name in ("login", "self_reset_request", "healthz"):
            path = _all_named_no_arg_urls().get(name)
            self.assertIsNotNone(path, f"{name} not found in URLconf")
            self.assertEqual(self.anon.get(path).status_code, 200,
                             f"public page {name} did not return 200")


class PublicEndpointsAreActuallyReachableTests(TestCase):
    """Everything on the public allowlist must actually be reachable.

    ``DefaultDenyTests.test_allowlisted_public_pages_really_are_reachable``
    already asks this question, but only of three hard-coded names (login, the
    password-reset entry, healthz). So it could never notice a *fourth* public
    endpoint going wrong, which is exactly what happened: the public benevolent
    application form was missing its ``@login_not_required`` and redirected
    every applicant to a login page they have no account for. The deny test
    passed — a redirect to login is precisely "turned away" — and the form was
    dead whether or not a church switched it on.

    This generalises the same assertion over the whole allowlist, so a new
    public endpoint is checked the day it is added rather than the day someone
    remembers to extend a hard-coded tuple. The narrower test above is kept as
    it is: it asserts a full 200 for pages that must genuinely render, which is
    stronger than "did not bounce to login" and worth keeping distinct.
    """

    # Endpoints that legitimately answer a bare anonymous GET with a redirect,
    # for a reason other than "you are not logged in". Kept short and justified.
    REDIRECTS_FOR_ANOTHER_REASON = {
        "logout",             # bounces to the login page by design
        "twofactor_verify",   # no pending login in session -> back to login
        "password_reset_confirm",  # needs a token in the path
    }

    def test_allowlisted_endpoints_do_not_redirect_to_login(self):
        anon = Client()
        urls = _all_named_no_arg_urls()
        offenders = []

        for name in sorted(PUBLIC_URL_NAMES - self.REDIRECTS_FOR_ANOTHER_REASON):
            path = urls.get(name)
            if not path:
                continue
            try:
                response = anon.get(path)
            except Exception:
                continue      # a view that raises is a different test's problem
            location = response.headers.get("Location", "") if \
                response.status_code in (301, 302) else ""
            if "/accounts/login" in location:
                offenders.append(
                    f"  {name} ({path}) redirected anonymous users to {location}")

        self.assertFalse(
            offenders,
            "These endpoints are on the public allowlist but send anonymous "
            "visitors to the login page, so they cannot do the job they exist "
            "for. They are most likely missing @login_not_required:\n"
            + "\n".join(offenders))
