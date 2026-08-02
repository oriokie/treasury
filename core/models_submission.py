"""One-time submission tokens: a form that is sent twice is only acted on once.

A treasurer clicks "Save all lines", nothing visibly happens for a second, and
they click again. Two identical POSTs arrive and two identical batches of
expenses are written. Nothing in the request tells the second one apart from a
genuine second batch — a church really may pay the same claimant the same
amount for the same thing twice on the same day — so the form itself has to
carry the distinction.

Each rendered form gets a token. The first POST claims it; a second POST
carrying the same token finds it already claimed and does nothing. The claim is
a unique row in the database rather than a cache entry, because the default
cache here is per-process and a church running more than one worker would get
no protection at all — which is exactly the deployment where the slow response
that causes the double click is most likely.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class FormSubmission(models.Model):
    """A form submission that has already been acted on."""

    token = models.CharField(max_length=32, unique=True, db_index=True)
    view = models.CharField(
        max_length=100, blank=True,
        help_text="Which form issued it — for reading the table, not for logic.")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL,
                             related_name="form_submissions")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.view or 'form'} {self.token[:8]} @ {self.created_at:%Y-%m-%d %H:%M}"

    @staticmethod
    def new_token():
        return uuid.uuid4().hex
