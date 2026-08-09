class SiteConfigCacheMiddleware:
    """Opens a request-scoped memo for ``SiteConfig.get()`` (recommendation #2,
    Option A): the first read in a request hits the database, every subsequent
    read reuses that object, and the memo is unconditionally dropped when the
    request ends — so no request can ever see another request's copy and
    nothing is cached across requests (see SiteConfig.get for why cross-request
    caching was deliberately rejected)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from core.models import _siteconfig_local
        _siteconfig_local.scope_open = True
        _siteconfig_local.obj = None
        try:
            return self.get_response(request)
        finally:
            _siteconfig_local.scope_open = False
            _siteconfig_local.obj = None


class PortalConfinementMiddleware:
    """Keeps a self-service member inside the portal.

    The application already denies by default (``LoginRequiredMiddleware``) and
    gates staff pages by role. Neither is sufficient here, and the difference
    matters.

    Role gating answers "may this login open this page". For the office roles
    that is the whole question, because a page either shows church-wide figures
    or it does not. For a portal member it is the wrong question entirely: the
    risk is not that they open the treasurer's dashboard, it is that they open
    any of the ~700 other views in this application which were written on the
    settled assumption that whoever reached them belongs to the office. Auditing
    every one of those, forever, is not a control anyone can keep true.

    So the rule is inverted for this role and enforced in one place: a login
    that is a portal member and nothing else may reach the portal, the account
    pages it needs to sign in and out and manage its own password, and nothing
    else. Everything else is a redirect, not a 403 — a member who follows a
    stale link should land somewhere useful rather than on an error.

    Note this deliberately triggers on ``is_portal_only`` (has the Member role,
    holds no office role) and not on ``is_portal_member`` (which additionally
    requires the account to be *usable*). A member whose portal account has
    been suspended must be confined too: the alternative is that suspending
    someone's portal access drops them into the office application.
    """

    # Prefixes a confined member may still reach. Everything here is either
    # authentication itself or a page about their own login — never church data.
    #
    # These must be checked against config/urls.py, not against what Django's
    # stock auth URLconf would have mounted. Two entries here were
    # "/accounts/password_reset" and "/accounts/reset", which are exactly the
    # stock names and match nothing in this application: the self-service flow
    # lives under /accounts/forgot-password/ (self_reset_request, its /verify/
    # step, and the emailed link's /email/confirm/<uidb64>/<token>/), and the
    # only "reset" route in accounts/urls.py is the administrator-triggered
    # /users/<pk>/reset-password/ — a different feature for a different role,
    # and one a member must certainly not reach. The effect was that the people
    # least able to ring the office for help got bounced back to the portal home
    # page the moment they clicked "forgot my password".
    #
    # One prefix covers all three legs of the flow because they share a path
    # root. Keep it that specific: broadening this to "/accounts/" would hand a
    # confined member the entire user-administration module.
    # core/test_portal_confinement_paths.py reverse()s the URL names and asks
    # this table about the results, so a route that moves again is caught.
    ALLOWED_PREFIXES = (
        "/portal/",
        "/accounts/login", "/accounts/logout",
        "/accounts/password_change", "/accounts/forgot-password",
        "/2fa/",
        "/static/", "/media/", "/healthz",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and self._confined(user) and not self._allowed(request.path):
            from django.shortcuts import redirect
            return redirect("portal_home")
        return self.get_response(request)

    @staticmethod
    def _confined(user):
        from core.roles import is_portal_only
        return is_portal_only(user)

    def _allowed(self, path):
        return any(path.startswith(p) for p in self.ALLOWED_PREFIXES)
