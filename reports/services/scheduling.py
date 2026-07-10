"""Report scheduling execution — run schedules to produce immutable snapshots.

Builds directly on the Snapshot Foundation (Phase 7): executing a schedule
renders its report for the policy-derived accounting period and creates a
finalised ``ReportSnapshot``, recording the outcome as a ``ScheduleRun`` (the
execution history). ``run_due_schedules`` is what a cron/worker would call; this
phase provides the execution machinery and manual execution, not the background
process itself.

Retry-on-failure is supported at the run level (an ``attempt`` counter); the
orchestration that re-invokes on failure is left to the operational scheduler,
which simply calls ``execute_schedule`` again.
"""
from __future__ import annotations

import datetime as _dt

from django.utils import timezone


def _period_for(policy, today=None):
    """Resolve a schedule's period policy to (start, end) dates."""
    today = today or _dt.date.today()
    y, m = today.year, today.month
    if policy == "prev_month":
        first_this = _dt.date(y, m, 1)
        end = first_this - _dt.timedelta(days=1)
        start = _dt.date(end.year, end.month, 1)
        return start, end
    if policy == "prev_quarter":
        q = (m - 1) // 3            # 0..3, current quarter index
        if q == 0:
            start = _dt.date(y - 1, 10, 1); end = _dt.date(y - 1, 12, 31)
        else:
            start = _dt.date(y, (q - 1) * 3 + 1, 1)
            last_m = q * 3
            end = _dt.date(y, last_m, 1) + _dt.timedelta(days=31)
            end = _dt.date(end.year, end.month, 1) - _dt.timedelta(days=1)
        return start, end
    if policy == "ytd":
        return _dt.date(y, 1, 1), today
    if policy == "prev_year":
        return _dt.date(y - 1, 1, 1), _dt.date(y - 1, 12, 31)
    return None, None               # all time


def _resolve_report(report_key):
    """Find an engine report by key — code-defined or a compiled definition."""
    from core.reporting import registry
    report = registry.get(report_key)
    if report is not None:
        return report
    # maybe it's a definition that isn't registered yet
    if report_key.startswith("def__"):
        from reports.models import ReportDefinition
        from reports.services.designer import register_definition
        d = ReportDefinition.objects.filter(
            key=report_key[len("def__"):], enabled=True).first()
        if d:
            return register_definition(d)
    return None


def execute_schedule(schedule, *, user=None, attempt=1):
    """Run one schedule now: render its report for the policy period and create
    an immutable snapshot. Records and returns a ScheduleRun. Never raises —
    failures are captured on the run so the scheduler can retry."""
    from reports.models import ScheduleRun
    from reports.services.snapshots import create_snapshot
    from core.reporting import ReportContext
    from core.models import SiteConfig

    run = ScheduleRun(schedule=schedule, attempt=attempt)
    report = _resolve_report(schedule.report_key)
    if report is None:
        run.status = ScheduleRun.Status.FAILED
        run.detail = f"Report '{schedule.report_key}' not found."
        run.save()
        _finish(schedule, run)
        return run

    try:
        start, end = _period_for(schedule.period_policy)
        ctx = ReportContext.for_period(start, end, label=report.title)
        # build a RenderedReport without a request (schedules run headless):
        rendered = _render_headless(report, ctx, user)
        church = SiteConfig.get().church_name
        formats = tuple(schedule.formats or ("csv",))
        snap = create_snapshot(rendered, user=user, formats=formats,
                               church=church)
        run.snapshot = snap
        run.status = ScheduleRun.Status.SUCCESS
        run.detail = f"Snapshot #{snap.id} created for {start}..{end}."
    except Exception as exc:  # noqa: BLE001 — capture for retry
        run.status = ScheduleRun.Status.FAILED
        run.detail = f"{type(exc).__name__}: {exc}"
    run.save()
    _finish(schedule, run)
    return run


def _render_headless(report, ctx, user):
    """Render an engine report without an HTTP request (for scheduled runs).
    Reproduces Report.render's pipeline but with a supplied context and no
    request-bound filters (defaults are used)."""
    from core.reporting import RenderedReport
    filters = {f.name: f.default for f in report.filters}
    sections = []
    for section in report.sections:
        if user is not None and not section.visible_to(user):
            continue
        data = section.build(ctx, filters)
        if data is not None:
            sections.append(data)
    return RenderedReport(report=report, context=ctx, filters=filters,
                          sections=sections)


def _finish(schedule, run):
    schedule.last_run = timezone.now()
    schedule.last_status = run.status
    schedule.next_run = schedule.compute_next_run(after=schedule.last_run)
    schedule.save(update_fields=["last_run", "last_status", "next_run"])


def run_due_schedules(now=None, *, user=None):
    """Execute every enabled schedule whose next_run is due. This is the entry
    point a cron/worker calls. Returns the list of ScheduleRuns produced."""
    from reports.models import ReportSchedule
    now = now or timezone.now()
    runs = []
    due = ReportSchedule.objects.filter(
        enabled=True, next_run__isnull=False, next_run__lte=now)
    for sched in due:
        runs.append(execute_schedule(sched, user=user))
    return runs
