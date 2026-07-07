"""Custom login view + middleware that weave TOTP into the auth flow."""
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.shortcuts import redirect

from .twofactor import PENDING_USER, VERIFIED


class LockAwareAuthenticationForm(AuthenticationForm):
    """Rejects a login for an account an administrator has locked
    (suspended) — a deliberate, short-term block distinct from is_active
    (which Django's own confirm_login_allowed already checks). Checked here,
    at the point of login, so a locked account can never authenticate
    through any path that reaches this form."""
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        profile = getattr(user, "profile", None)
        if profile and profile.locked:
            raise ValidationError(
                "This account has been suspended by an administrator. "
                "Contact your treasurer for details.",
                code="account_locked")


class TwoFactorLoginView(LoginView):
    """Standard username/password login. If the authenticated user has confirmed
    two-factor, we DON'T fully log them in yet — we stash their id and bounce to
    the TOTP gate. Axes still protects the password step."""
    template_name = "registration/login.html"
    authentication_form = LockAwareAuthenticationForm

    def form_valid(self, form):
        user = form.get_user()
        tf = getattr(user, "two_factor", None)
        if tf and tf.confirmed:
            # hold the login: record pending id, do not call login()
            from .twofactor import ATTEMPTS
            self.request.session[PENDING_USER] = user.pk
            self.request.session[VERIFIED] = False
            self.request.session.pop(ATTEMPTS, None)
            nxt = self.get_redirect_url()
            if nxt:
                self.request.session["2fa_next"] = nxt
            return redirect("twofactor_verify")
        # no 2FA — normal login
        return super().form_valid(form)


class AccountLockMiddleware:
    """If an administrator locks (suspends) a user who already has an active
    session, end that session on their very next request rather than waiting
    for them to log in again — a lock is meant to take effect immediately.
    Checked before the 2FA gate, so a locked account is stopped at the door
    regardless of its 2FA state."""
    EXEMPT_PREFIXES = ("/accounts/login", "/accounts/logout", "/static/",
                       "/healthz", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (user and user.is_authenticated and not user.is_superuser
                and not self._exempt(request.path)):
            profile = getattr(user, "profile", None)
            if profile and profile.locked:
                from django.contrib.auth import logout
                logout(request)
                # messages isn't guaranteed to be set up this early in the
                # middleware chain, so the login page itself shows the notice
                # via this query flag rather than the messages framework
                return redirect("/accounts/login/?locked=1")
        return self.get_response(request)

    def _exempt(self, path):
        return any(path.startswith(p) for p in self.EXEMPT_PREFIXES)


class ForcePasswordChangeMiddleware:
    """If an administrator has flagged an account for a forced password
    change (UserProfile.must_change_password), redirect every request to the
    password-change form until they've changed it. Mirrors
    TwoFactorMiddleware's exemption pattern so the change-password page,
    login/logout, and static/media assets always remain reachable. The flag
    is cleared automatically by _track_password_change (below) the moment
    the password actually changes, regardless of which view did it."""
    EXEMPT_PREFIXES = ("/accounts/login", "/accounts/logout", "/accounts/password_change",
                       "/2fa/", "/static/", "/healthz", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and not self._exempt(request.path):
            profile = getattr(user, "profile", None)
            if profile and profile.must_change_password:
                return redirect("password_change")
        return self.get_response(request)

    def _exempt(self, path):
        return any(path.startswith(p) for p in self.EXEMPT_PREFIXES)


class TwoFactorMiddleware:
    """Enforces two-factor where required:

    * If a user has confirmed 2FA, their session must be VERIFIED to use the app.
    * If SiteConfig.require_2fa_for_treasurers is on and the user is a treasurer
      without 2FA set up, force them to the setup page until they enrol.

    Login, logout, static, healthcheck and the 2FA pages themselves are exempt so
    a user can always complete or recover the flow.
    """
    EXEMPT_PREFIXES = ("/accounts/login", "/accounts/logout", "/2fa/",
                       "/static/", "/healthz", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        user = getattr(request, "user", None)
        if user and user.is_authenticated and not self._exempt(path):
            tf = getattr(user, "two_factor", None)
            # 1) confirmed 2FA but session not verified → must verify
            if tf and tf.confirmed and not request.session.get(VERIFIED):
                uid = user.pk
                from django.contrib.auth import logout
                # drop the auth so they can't act unverified, then send to gate.
                # logout() flushes the session, so set the pending id AFTER it.
                logout(request)
                request.session[PENDING_USER] = uid
                return redirect("twofactor_verify")
            # 2) treasurer required to enrol but hasn't
            if (not tf or not tf.confirmed) and self._enrolment_required(user):
                if not path.startswith("/2fa/"):
                    return redirect("twofactor_setup")
        return self.get_response(request)

    def _exempt(self, path):
        return any(path.startswith(p) for p in self.EXEMPT_PREFIXES)

    def _enrolment_required(self, user):
        try:
            from core.models import SiteConfig
            from core.roles import is_treasurer
            return SiteConfig.get().require_2fa_for_treasurers and is_treasurer(user)
        except Exception:
            return False


def axes_lockout_response(request, *args, **kwargs):
    """Called by django-axes instead of its own bare, unstyled lockout page.
    Redirects back to the app's own login page with a query flag, which
    renders a clear message in the app's normal styling — the same pattern
    already used for an administrator-suspended account (?locked=1), kept as
    a visually distinct flag (?axeslocked=1) since the two situations have
    different remedies: an admin-suspended account needs a treasurer to
    reinstate it, while a failed-attempts lockout just needs a short wait."""
    return redirect("/accounts/login/?axeslocked=1")
