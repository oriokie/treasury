"""Report snapshot foundation — the data model for immutable, versioned report
snapshots.

This phase implements the *architecture* only: a place to persist a rendered
report as an immutable record, with the metadata a later phase needs to schedule
generation, verify integrity and compare versions. It does NOT add scheduling and
does NOT change how any report renders today — a snapshot is created only when
explicitly requested via the snapshot service.

A snapshot captures which report + accounting period it covers; when it was
generated and by whom; the report and template/engine version at generation
time; the rendered payload plus a per-format export checksum; and rendering
metadata (filters, metrics used, component keys) for provenance.

Immutability is enforced at the application layer: once ``finalised`` a snapshot
refuses further edits. Retention/scheduling is a later phase.
"""
from __future__ import annotations

import hashlib

from django.conf import settings
from django.db import models


def compute_checksum(content) -> str:
    """SHA-256 of bytes or text — the canonical checksum for an export payload,
    so a regenerated export can be compared byte-for-byte to the snapshot."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


class ReportSnapshot(models.Model):
    """An immutable, versioned capture of a report for an accounting period.

    Created on demand by ``reports.services.snapshots.create_snapshot``. Once
    ``finalised`` it cannot be modified — the record is the permanent evidence of
    what the report showed at generation time.
    """

    # identity: which report, which period
    report_key = models.CharField(max_length=100, db_index=True)
    report_title = models.CharField(max_length=200)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True, db_index=True)

    # generation metadata
    generated_at = models.DateTimeField(auto_now_add=True, db_index=True)
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                     blank=True, on_delete=models.SET_NULL,
                                     related_name="report_snapshots")

    # versioning
    report_version = models.CharField(
        max_length=20, default="",
        help_text="Application VERSION at generation time.")
    template_version = models.CharField(
        max_length=40, default="",
        help_text="Report/engine template version — bump when a report's "
                  "structure changes so snapshots are comparable within a version.")
    schema_version = models.PositiveSmallIntegerField(
        default=1, help_text="Snapshot payload schema version.")

    # payload + integrity
    payload = models.JSONField(
        default=dict,
        help_text="The rendered report as structured data (sections → rows).")
    checksums = models.JSONField(
        default=dict,
        help_text="Per-format export checksums, e.g. {'csv': '…', 'xlsx': '…'}.")

    # rendering metadata (provenance)
    filters = models.JSONField(default=dict, blank=True)
    metrics_used = models.JSONField(default=list, blank=True)
    component_keys = models.JSONField(default=list, blank=True)
    render_meta = models.JSONField(default=dict, blank=True)

    finalised = models.BooleanField(
        default=False,
        help_text="Once true, the snapshot is immutable and cannot be re-saved.")

    class Meta:
        ordering = ["-generated_at"]
        indexes = [
            models.Index(fields=["report_key", "period_end"]),
            models.Index(fields=["report_key", "generated_at"]),
        ]

    def __str__(self):
        return f"{self.report_key} @ {self.period_end or 'all'} " \
               f"({self.generated_at:%Y-%m-%d %H:%M})"

    # immutability guard
    def save(self, *args, **kwargs):
        if self.pk:
            stored = ReportSnapshot.objects.filter(pk=self.pk,
                                                   finalised=True).first()
            if stored is not None:
                raise ValueError(
                    "ReportSnapshot is finalised and immutable; create a new "
                    "snapshot instead of editing this one.")
        super().save(*args, **kwargs)

    def add_checksum(self, fmt, content):
        """Record the checksum for one export format (only before finalising)."""
        if self.finalised:
            raise ValueError("Cannot add a checksum to a finalised snapshot.")
        self.checksums[fmt] = compute_checksum(content)

    def matches(self, fmt, content) -> bool:
        """Whether a (re)generated export for ``fmt`` matches this snapshot —
        the integrity/verification primitive a later scheduling phase uses."""
        stored = self.checksums.get(fmt)
        return bool(stored) and stored == compute_checksum(content)

    def metadata(self) -> dict:
        """A compact, serialisable metadata header (no payload)."""
        return {
            "report_key": self.report_key, "report_title": self.report_title,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "report_version": self.report_version,
            "template_version": self.template_version,
            "schema_version": self.schema_version,
            "checksums": dict(self.checksums),
            "metrics_used": list(self.metrics_used),
            "finalised": self.finalised,
        }


# Admin/designer/scheduling/branding models (kept in a separate module for
# readability; imported here so Django's app loader discovers them).
from reports.models_admin import (  # noqa: E402,F401
    ReportBranding, ReportDefinition, ReportSchedule, ScheduleRun,
    ReportUsage, ReportFavourite,
)
from reports.models_narrative import ReportNarrative  # noqa: E402,F401
