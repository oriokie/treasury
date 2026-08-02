"""Act on a form submission once, however many times it arrives.

Usage in a view::

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        ctx["submit_token"] = idempotency.issue()
        return ctx

    def post(self, request):
        if not idempotency.claim(request, view="expense_batch"):
            messages.info(request, "That was already saved.")
            return redirect(...)
        ...

``issue`` is called on the render, ``claim`` on the post. A second post
carrying the same token returns False and the view returns early, so the double
click that produced it costs nothing.
"""
from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction as db_tx
from django.utils import timezone

#: The form field carrying the token.
FIELD = "submit_token"

#: Claims older than this are pruned; long enough that no plausible retry of a
#: submission is still in flight, short enough that the table stays small.
RETENTION = dt.timedelta(days=30)


def issue():
    """A fresh token for a form being rendered."""
    from core.models import FormSubmission
    return FormSubmission.new_token()


def claim(request, view=""):
    """Claim this submission. True the first time, False on a repeat.

    A form with no token is always allowed through: the guard is opt-in per
    form, and a missing token must never block a legitimate save (an older
    cached page, a form this has not been added to yet, a scripted client).
    """
    from core.models import FormSubmission
    token = (request.POST.get(FIELD) or "").strip()[:32]
    if not token:
        return True
    user = getattr(request, "user", None)
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    try:
        # The unique index is what actually decides, so two requests racing
        # each other in separate workers cannot both win. Its own transaction,
        # so losing the race does not poison an outer atomic block.
        with db_tx.atomic():
            FormSubmission.objects.create(token=token, view=view[:100],
                                          user=user)
    except IntegrityError:
        return False
    _prune()
    return True


def _prune():
    """Drop claims old enough that nothing could still be retrying them. Best
    effort — housekeeping must never fail a save."""
    from core.models import FormSubmission
    try:
        cutoff = timezone.now() - RETENTION
        stale = FormSubmission.objects.filter(created_at__lt=cutoff)
        if stale.exists():
            stale.delete()
    except Exception:  # noqa: BLE001
        pass
