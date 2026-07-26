"""How negative figures are written, for the duration of one request.

A template filter cannot see the request, so the user's choice has to be put
somewhere the filter can reach. This is a `ContextVar` set by middleware at the
start of each request and cleared at the end — the same shape as the
request-scoped memo in `core.perfcache`, and safe under threads, async and the
test client alike, which a module-level global would not be.

Deliberately narrow: it carries one presentation setting, never data. If it is
unset — a management command, a Celery job, a shell — `negatives_style()`
returns the default, so nothing that runs outside a request has to care.
"""
from contextvars import ContextVar

_NEGATIVES = ContextVar("negatives_style", default="MINUS")


def negatives_style():
    """"MINUS" or "PARENS" for the current request."""
    return _NEGATIVES.get() or "MINUS"


def set_negatives_style(style):
    return _NEGATIVES.set(style or "MINUS")


def reset_negatives_style(token):
    _NEGATIVES.reset(token)


class NumberStyleMiddleware:
    """Publish the signed-in user's negative-number preference for this request.

    Runs on every request because figures are rendered on nearly every page;
    the cost is one already-cached preference read. Anonymous users and requests
    made before the preference tables exist fall back to the default.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        style = "MINUS"
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            try:
                from core.models import UserPreference
                style = UserPreference.get_for(user).negatives
            except Exception:
                style = "MINUS"
        token = set_negatives_style(style)
        try:
            return self.get_response(request)
        finally:
            reset_negatives_style(token)
