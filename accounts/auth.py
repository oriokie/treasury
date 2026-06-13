"""Custom login view + middleware that weave TOTP into the auth flow."""
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect

from .twofactor import PENDING_USER, VERIFIED


class TwoFactorLoginView(LoginView):
    """Standard username/password login. If the authenticated user has confirmed
    two-factor, we DON'T fully log them in yet — we stash their id and bounce to
    the TOTP gate. Axes still protects the password step."""
    template_name = "registration/login.html"

    def form_valid(self, form):
        user = form.get_user()
        tf = getattr(user, "two_factor", None)
        if tf and tf.confirmed:
            # hold the login: record pending id, do not call login()
            self.request.session[PENDING_USER] = user.pk
            self.request.session[VERIFIED] = False
            nxt = self.get_redirect_url()
            if nxt:
                self.request.session["2fa_next"] = nxt
            return redirect("twofactor_verify")
        # no 2FA — normal login
        return super().form_valid(form)


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
