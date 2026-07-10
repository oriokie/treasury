"""Report administration models — the persistence layer for the configuration-
driven reporting platform (Report Designer, Scheduling, Branding, Library).

These models let administrators design, schedule and brand reports *as data*,
which the existing Generic Report Engine renders. No report code changes to add
or re-lay-out a report: a ``ReportDefinition`` is compiled into a live engine
``Report`` by ``reports.services.designer.compile_definition``.

Nothing here changes how the existing code-defined reports render; definitions
are an additional, parallel source of reports. Accounting still flows only
through the Financial Metrics Registry via ReportContext — a definition can only
arrange *registered* components, never introduce a calculation.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


# ===========================================================================
# Branding / themes
# ===========================================================================

class ReportBranding(models.Model):
    """Organisation branding applied to rendered reports. A single active
    branding is used by default; renderers read it to stamp headers, footers,
    logos, colours and certification statements."""
    name = models.CharField(max_length=100, default="Default")
    is_active = models.BooleanField(default=False, db_index=True)

    church_name = models.CharField(max_length=200, blank=True)
    conference = models.CharField(max_length=200, blank=True)
    region = models.CharField(max_length=200, blank=True)
    contact_details = models.CharField(max_length=300, blank=True)

    logo_url = models.URLField(blank=True)
    primary_colour = models.CharField(max_length=7, default="#1f5f4f")
    accent_colour = models.CharField(max_length=7, default="#b08d57")
    font_family = models.CharField(max_length=120,
                                   default="Calibri, Arial, sans-serif")

    header_text = models.CharField(max_length=300, blank=True)
    footer_text = models.CharField(max_length=300, blank=True)
    watermark_text = models.CharField(max_length=60, blank=True)
    certification_statement = models.TextField(blank=True)

    page_size = models.CharField(max_length=10, default="A4")
    page_orientation = models.CharField(
        max_length=10, default="portrait",
        choices=[("portrait", "Portrait"), ("landscape", "Landscape")])
    show_page_numbers = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_active", "name"]

    def __str__(self):
        return f"{self.name}{' (active)' if self.is_active else ''}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # only one active branding at a time
        if self.is_active:
            ReportBranding.objects.exclude(pk=self.pk).filter(
                is_active=True).update(is_active=False)

    @classmethod
    def active(cls):
        return cls.objects.filter(is_active=True).first()

    def as_dict(self):
        return {
            "church_name": self.church_name, "conference": self.conference,
            "region": self.region, "contact_details": self.contact_details,
            "logo_url": self.logo_url, "primary_colour": self.primary_colour,
            "accent_colour": self.accent_colour, "font_family": self.font_family,
            "header_text": self.header_text, "footer_text": self.footer_text,
            "watermark_text": self.watermark_text,
            "certification_statement": self.certification_statement,
            "page_size": self.page_size,
            "page_orientation": self.page_orientation,
            "show_page_numbers": self.show_page_numbers,
        }


# ===========================================================================
# Report definitions (the Designer's saved templates)
# ===========================================================================

class ReportDefinition(models.Model):
    """A persisted, admin-designed report template. Compiled into a live engine
    ``Report`` on demand. The section list is stored as JSON — each entry names
    a registered component, its parameters (e.g. ``narrative_key``), a title
    override, an enabled flag, and a ``LayoutMeta`` dict.

    Storing sections as data (not code) is what makes the platform
    configuration-driven: reordering, grouping, toggling and re-laying-out a
    report is an edit to this row, never a code change.
    """
    key = models.SlugField(
        max_length=100, unique=True,
        help_text="URL-safe id; the report renders at /reports/r/def__<key>/.")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=80, default="Custom")

    # sections: list of {component, params, title, enabled, layout}
    sections = models.JSONField(default=list)
    # report-level filters: list of {name, label, kind, default}
    filters = models.JSONField(default=list, blank=True)

    # designer page settings (cover/toc/orientation/etc.)
    page_settings = models.JSONField(default=dict, blank=True)

    # access: minimum role required, mirrors the report permission
    PERMISSION_CHOICES = [
        ("reports", "Anyone with report access"),
        ("treasurer", "Treasurer only"),
        ("admin", "Administrators only"),
    ]
    permission = models.CharField(max_length=20, default="reports",
                                  choices=PERMISSION_CHOICES)

    enabled = models.BooleanField(default=True)
    template_version = models.PositiveIntegerField(default=1)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.SET_NULL,
                              related_name="report_definitions")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # library metadata
    tags = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["category", "title"]

    def __str__(self):
        return f"{self.title} ({self.key})"

    @property
    def engine_key(self):
        """The key the compiled report registers under — namespaced so a
        definition can never clash with a code-defined report."""
        return f"def__{self.key}"

    def bump_version(self):
        self.template_version += 1


# ===========================================================================
# Scheduling
# ===========================================================================

class ReportSchedule(models.Model):
    """A schedule for automatic report generation + snapshotting. Building on
    the Snapshot Foundation: each run creates an immutable snapshot and records
    execution history. This phase implements the schedule model, next-run
    computation and manual/'due' execution; a background worker (cron/celery) is
    a later operational step that simply calls ``run_due_schedules``.
    """
    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"
        QUARTERLY = "QUARTERLY", "Quarterly"
        YEARLY = "YEARLY", "Yearly"
        MANUAL = "MANUAL", "Manual only"

    name = models.CharField(max_length=150)
    report_key = models.CharField(
        max_length=100,
        help_text="Engine report key (code-defined or def__<key>).")
    frequency = models.CharField(max_length=10, choices=Frequency.choices,
                                 default=Frequency.MONTHLY)
    formats = models.JSONField(default=list,
                               help_text="Export formats to snapshot, e.g. ['csv'].")

    # period policy: which accounting period each run covers
    PERIOD_CHOICES = [
        ("prev_month", "Previous month"),
        ("prev_quarter", "Previous quarter"),
        ("ytd", "Year to date"),
        ("prev_year", "Previous year"),
        ("all", "All time"),
    ]
    period_policy = models.CharField(max_length=20, default="prev_month",
                                     choices=PERIOD_CHOICES)

    enabled = models.BooleanField(default=True, db_index=True)
    next_run = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=20, blank=True)

    # distribution
    recipients = models.JSONField(
        default=list, blank=True,
        help_text="Email addresses to notify when a snapshot is generated.")
    require_approval = models.BooleanField(default=False)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} [{self.get_frequency_display()}]"

    def compute_next_run(self, after=None):
        """Next run datetime after ``after`` (default: now), by frequency.
        Simple, deterministic stepping — no timezone gymnastics beyond the
        project default; a manual schedule has no next run."""
        import datetime as _dt
        from django.utils import timezone
        base = after or timezone.now()
        if self.frequency == self.Frequency.MANUAL:
            return None
        step = {
            self.Frequency.DAILY: _dt.timedelta(days=1),
            self.Frequency.WEEKLY: _dt.timedelta(weeks=1),
            self.Frequency.MONTHLY: _dt.timedelta(days=30),
            self.Frequency.QUARTERLY: _dt.timedelta(days=91),
            self.Frequency.YEARLY: _dt.timedelta(days=365),
        }[self.frequency]
        return base + step


class ScheduleRun(models.Model):
    """One execution of a schedule — the execution history / audit trail."""
    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    schedule = models.ForeignKey(ReportSchedule, on_delete=models.CASCADE,
                                 related_name="runs")
    started_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    snapshot = models.ForeignKey("reports.ReportSnapshot", null=True, blank=True,
                                 on_delete=models.SET_NULL)
    detail = models.TextField(blank=True)
    attempt = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.schedule.name} @ {self.started_at:%Y-%m-%d %H:%M} " \
               f"[{self.status}]"


# ===========================================================================
# Library: favourites & usage tracking
# ===========================================================================

class ReportUsage(models.Model):
    """Lightweight per-report usage tracking for the Library's 'recently used'
    and 'frequently used', and for the adoption dashboard's generation stats."""
    report_key = models.CharField(max_length=100, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL)
    viewed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    render_ms = models.PositiveIntegerField(default=0)
    export_format = models.CharField(max_length=10, blank=True)

    class Meta:
        ordering = ["-viewed_at"]
        indexes = [models.Index(fields=["report_key", "viewed_at"])]


class ReportFavourite(models.Model):
    report_key = models.CharField(max_length=100)
    user = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.CASCADE,
                             related_name="report_favourites")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("report_key", "user")]
        ordering = ["report_key"]
