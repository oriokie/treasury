"""Editable report narration.

Every section of an engine report carries an explanation. By default that
explanation is generated — deterministically from the same figures the section
shows, or by the configured AI assistant when one is switched on. A treasurer
who wants to say it differently can edit it, and the edit is what the board
sees and what prints.

An override is stored per report, per section and per period, so editing the
July commentary never rewrites June's. Clearing the text restores the generated
explanation rather than blanking the section.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ReportNarrative(models.Model):
    """A treasurer's own words for one section of one report for one period."""

    class Source(models.TextChoices):
        AUTO = "AUTO", "Generated from the figures"
        AI = "AI", "Written by the AI assistant"
        MANUAL = "MANUAL", "Edited by the treasurer"

    report_key = models.CharField(max_length=100, db_index=True)
    section_key = models.CharField(max_length=100)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    text = models.TextField(blank=True)
    source = models.CharField(max_length=8, choices=Source.choices,
                              default=Source.MANUAL)

    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   blank=True, on_delete=models.SET_NULL,
                                   related_name="report_narratives")
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report_key", "section_key", "period_start", "period_end"],
                name="uniq_report_narrative_period"),
        ]
        indexes = [models.Index(fields=["report_key", "period_end"])]
        ordering = ["report_key", "section_key"]

    def __str__(self):
        return f"{self.report_key}/{self.section_key} " \
               f"({self.period_start or 'all'} – {self.period_end or 'all'})"
