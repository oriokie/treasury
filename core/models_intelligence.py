"""Persistence for insight/recommendation status — the dismissal audit trail.

Insights are computed live (deterministically) from the metrics each time; this
model records only the *status* a user has assigned to a given insight
(acknowledged/resolved/dismissed) keyed by the insight's stable fingerprint, plus
who changed it and when. It never stores a financial figure — the figures always
come from the registry — so there is no risk of a persisted number drifting from
the accounting truth.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class InsightStatus(models.Model):
    """A user-assigned status for an insight, keyed by its fingerprint
    (code:subject:period). Provides the dismissible-with-audit-history behaviour
    for insights and recommendations."""

    class State(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    fingerprint = models.CharField(max_length=200, unique=True, db_index=True)
    code = models.CharField(max_length=60, db_index=True)
    subject = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=14, choices=State.choices,
                             default=State.OPEN)
    note = models.CharField(max_length=300, blank=True)

    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.code} [{self.state}]"


class InsightStatusHistory(models.Model):
    """Append-only audit trail of insight status changes."""
    status = models.ForeignKey(InsightStatus, on_delete=models.CASCADE,
                               related_name="history")
    state = models.CharField(max_length=14)
    note = models.CharField(max_length=300, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   blank=True, on_delete=models.SET_NULL)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]
