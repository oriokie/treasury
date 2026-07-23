"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from decimal import Decimal
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from cashbook.models import Expense
from ..services import balances
import datetime as _dt
from django.utils import timezone as _tz
from cashbook.models import RemittanceBatch
from core.models import SiteConfig
from core.utils import sabbath_week_of
from ..services.remittance import (                          # noqa: E402
    days_outstanding as _days_outstanding,
    repost_to_ledger as _repost_to_ledger,
    remittance_dashboard_rows)
from ._shared import PeriodMixin


class TrustFundView(PeriodMixin, TemplateView):
    template_name = "reports/trust.html"

    def get_context_data(self, **kwargs):
        from cashbook.models import RemittanceBatch
        ctx = super().get_context_data(**kwargs)
        ctx["rows"] = balances.trust_summary(ctx["start"], ctx["end"])
        ctx["total_to_remit"] = sum(r["to_remit"] for r in ctx["rows"])
        ctx["total_unreceipted"] = sum((r["unreceipted"] for r in ctx["rows"]), Decimal(0))
        ctx["total_liability"] = sum((r["total_liability"] for r in ctx["rows"]), Decimal(0))
        ctx["batches"] = (RemittanceBatch.objects
                          .order_by("-date", "-id")[:25])
        ctx["remitted_total"] = sum(r["remitted"] for r in ctx["rows"])
        return ctx

class RemittanceView(PeriodMixin, TemplateView):
    template_name = "reports/remittance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["rows"] = balances.trust_summary(ctx["start"], ctx["end"])
        ctx["total"] = sum(r["to_remit"] for r in ctx["rows"])
        ctx["total_unreceipted"] = sum((r["unreceipted"] for r in ctx["rows"]), Decimal(0))
        from statements.models import BankAccount
        ctx["bank_accounts"] = BankAccount.objects.all()
        return ctx

class RemitTrustView(TreasurerRequiredMixin, View):
    """Remit the amount still outstanding for each trust fund in the period as a
    single field payment. This now uses the same payment architecture as the
    batch workflow: it creates a remittance batch, raises the per-fund expenses
    against it, and settles the whole batch with one PaymentInstrument (cheque,
    EFT, RTGS, M-Pesa, etc.). The instrument is the settlement record; it posts
    no separate accounting entries."""

    def post(self, request):
        import datetime as _dt
        from cashbook.models import (Expense, RemittanceBatch, PaymentInstrument)
        from core.models import SiteConfig
        from core.utils import sabbath_week_of
        try:
            s = _dt.date.fromisoformat(request.POST["start"])
            e = _dt.date.fromisoformat(request.POST["end"])
        except (KeyError, ValueError):
            messages.error(request, "Pick a valid period to remit.")
            return redirect("report_remittance")
        field = SiteConfig.get().field_name or "the field"

        method = (request.POST.get("method") or "CHEQUE").upper()
        valid_methods = dict(PaymentInstrument.Method.choices)
        if method not in valid_methods:
            method = "CHEQUE"
        reference = (request.POST.get("instrument_number") or "").strip()
        try:
            paid = (_dt.date.fromisoformat(request.POST.get("date_issued"))
                    if request.POST.get("date_issued") else e)
        except ValueError:
            paid = e
        bank_id = request.POST.get("bank_account") or ""

        rows = balances.trust_summary(s, e)
        outstanding = [(r["department"], r["to_remit"]) for r in rows
                       if r["to_remit"] and r["to_remit"] > 0]
        if not outstanding:
            messages.info(request, "Nothing outstanding to remit for this period.")
            return redirect(f"{reverse('report_remittance')}?start={s}&end={e}")

        total = sum((amt for _, amt in outstanding), Decimal(0))
        batch = RemittanceBatch.create_batch(
            total_amount=total, status=RemittanceBatch.Status.REMITTED,
            period_start=s, period_end=e, created_by=request.user,
            approved_by=request.user, remitted_at=_tz.now())

        # one settlement instrument for the whole field payment
        inst = PaymentInstrument(
            method=method, instrument_number=reference[:40],
            payee=field, amount=total, date_issued=paid,
            status=PaymentInstrument.Status.ISSUED,
            source_kind=PaymentInstrument.SourceKind.REMITTANCE,
            remittance_batch=batch, recorded_by=request.user)
        if bank_id.isdigit():
            from statements.models import BankAccount
            inst.bank_account = BankAccount.objects.filter(pk=bank_id).first()
        inst.save()
        batch.payment = inst
        if method == "CHEQUE":          # keep legacy fields in step for old reports
            batch.cheque_no = reference[:30]
            batch.cheque_date = paid
        batch.save(update_fields=["payment", "cheque_no", "cheque_date"])

        for dept, amt in outstanding:
            Expense.objects.create(
                date=paid, sabbath_week=sabbath_week_of(paid), department=dept,
                description=f"Remittance to {field} ({s:%d %b}-{e:%d %b %Y})",
                amount=amt, category=Expense.Category.REMITTANCE,
                claimant=field, method=Expense.Method.CHEQUE,
                voucher_no=reference[:30], status=Expense.Status.PAID,
                paid_date=paid, remittance_batch=batch,
                recorded_by=request.user, approved_by=request.user)

        messages.success(
            request, f"Remitted {len(outstanding)} trust fund(s) totalling "
                     f"KES {total:,.2f} to {field}, settled by "
                     f"{inst.get_method_display()} {reference}." if reference else
                     f"Remitted {len(outstanding)} trust fund(s) totalling "
                     f"KES {total:,.2f} to {field} by {inst.get_method_display()}.")
        return redirect(f"{reverse('report_remittance')}?start={s}&end={e}")

def _remit_period(request):
    """Resolve a remittance period from the request: ?month=YYYY-MM, or
    ?start=&end=, else None (lifetime)."""
    import datetime as _dt, calendar as _cal
    month = request.GET.get("month") or request.POST.get("month")
    if month:
        try:
            y, m = (int(x) for x in month.split("-"))
            last = _cal.monthrange(y, m)[1]
            return _dt.date(y, m, 1), _dt.date(y, m, last), month
        except (ValueError, TypeError):
            pass
    s = request.GET.get("start") or request.POST.get("start")
    e = request.GET.get("end") or request.POST.get("end")
    def pd(v):
        try:
            return _dt.datetime.strptime(v, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    return pd(s), pd(e), None

class RemittanceDashboardView(ReportAccessMixin, TemplateView):
    template_name = "reports/remittance_dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end, month = _remit_period(self.request)
        rows = remittance_dashboard_rows(start, end)
        ctx["rows"] = rows
        ctx["period_start"], ctx["period_end"], ctx["period_month"] = start, end, month
        ctx["period_active"] = bool(start or end)
        ctx["total_outstanding"] = sum((r["outstanding"] for r in rows), Decimal(0))
        ctx["total_collected"] = sum((r["collected"] for r in rows), Decimal(0))
        ctx["total_remitted"] = sum((r["remitted"] for r in rows), Decimal(0))
        ctx["total_unreceipted"] = sum((r["unreceipted"] for r in rows), Decimal(0))
        ctx["max_days"] = max([r["days"] for r in rows], default=0)
        # Next remittance: use the configured per-month deadlines if any exist
        # (their dates are set freely per month, not on a fixed day). We count
        # down to the *reporting Sabbath* — the Saturday whose count must be in
        # the remittance. Fall back to the configured due-day only if no
        # deadlines have been entered yet.
        import datetime as _dt, calendar as _cal
        from cashbook.models import RemittanceDeadline
        today = _dt.date.today()
        nxt = (RemittanceDeadline.objects.filter(remitted=False, deadline__gte=today)
               .order_by("deadline").first())
        overdue_dl = (RemittanceDeadline.objects.filter(remitted=False, deadline__lt=today)
                      .order_by("-deadline").first())
        active = nxt or overdue_dl
        if active:
            ctx["next_deadline"] = active
            ctx["due_date"] = active.deadline
            ctx["reporting_sabbath"] = active.reporting_sabbath
            ctx["days_to_deadline"] = (active.deadline - today).days
            ctx["days_to_sabbath"] = (active.reporting_sabbath - today).days
            ctx["deadline_period"] = active.get_period_display()
            ctx["has_deadlines"] = True
        else:
            # legacy fallback: configured day of the following month
            cfg = SiteConfig.get()
            ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            due_day = min(cfg.trust_remit_due_day or 15, _cal.monthrange(ny, nm)[1])
            ctx["due_date"] = _dt.date(ny, nm, due_day)
            ctx["days_to_deadline"] = (ctx["due_date"] - today).days
            ctx["has_deadlines"] = False
        ctx["due_overdue"] = ctx["total_outstanding"] > 0 and ctx.get("days_to_deadline", 99) < 0
        ctx["batches"] = RemittanceBatch.objects.all()[:10]
        ctx["field_name"] = SiteConfig.get().field_name or "the field"
        return ctx

class RemittanceBatchCreateView(TreasurerRequiredMixin, View):
    """Generate a DRAFT batch. POST 'all'=1 to include every outstanding trust
    fund, or one or more 'fund' ids for a per-fund wizard."""
    def post(self, request):
        start, end, month = _remit_period(request)
        rows = {r["department"].id: r for r in remittance_dashboard_rows(start, end)}
        if request.POST.get("all"):
            chosen = [r for r in rows.values() if r["outstanding"] > 0]
        else:
            ids = [int(i) for i in request.POST.getlist("fund") if i.isdigit()]
            chosen = [rows[i] for i in ids if i in rows and rows[i]["outstanding"] > 0]
        if not chosen:
            messages.error(request, "No outstanding trust funds to remit for that period.")
            return redirect("remittance_dashboard")
        field = SiteConfig.get().field_name or "the field"
        batch = RemittanceBatch.create_batch(
            created_by=request.user, status=RemittanceBatch.Status.DRAFT,
            period_start=start, period_end=end)
        today = _dt.date.today()
        # date the remittance expense within the period it covers, so it ties to
        # the right month's trust collection
        rdate = end or today
        plabel = (f" for {month}" if month else
                  f" for {start:%d %b %Y}–{end:%d %b %Y}" if (start and end) else "")
        for r in chosen:
            Expense.objects.create(
                date=rdate, sabbath_week=sabbath_week_of(rdate), department=r["department"],
                description=f"Trust remittance to {field} — {batch.batch_number}{plabel}",
                amount=r["outstanding"], category=Expense.Category.REMITTANCE,
                claimant=field, method=Expense.Method.CHEQUE,
                status=Expense.Status.PENDING, recorded_by=request.user,
                remittance_batch=batch)
        batch.recompute_total()
        batch.save(update_fields=["total_amount"])
        messages.success(request, f"Created remittance batch {batch.batch_number} "
                                  f"covering {len(chosen)} fund(s){plabel}.")
        return redirect("remittance_batch_detail", pk=batch.pk)

class RemittanceBatchDetailView(ReportAccessMixin, TemplateView):
    template_name = "reports/remittance_batch.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        batch = get_object_or_404(RemittanceBatch, pk=kwargs["pk"])
        ctx["batch"] = batch
        ctx["lines"] = batch.expenses.select_related("department").all()
        ctx["field_name"] = SiteConfig.get().field_name or "the field"
        from statements.models import BankAccount
        ctx["bank_accounts"] = BankAccount.objects.all()
        return ctx

class RemittanceBatchApproveView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        batch = get_object_or_404(RemittanceBatch, pk=pk)
        if batch.status != RemittanceBatch.Status.DRAFT:
            messages.error(request, "Only a draft batch can be approved.")
            return redirect("remittance_batch_detail", pk=pk)
        batch.status = RemittanceBatch.Status.APPROVED
        batch.approved_by = request.user
        batch.save(update_fields=["status", "approved_by"])
        batch.expenses.update(status=Expense.Status.APPROVED, approved_by=request.user)
        _repost_to_ledger(batch.expenses.all())
        messages.success(request, f"Batch {batch.batch_number} approved.")
        return redirect("remittance_batch_detail", pk=pk)

class RemittanceBatchRemitView(TreasurerRequiredMixin, View):
    """Mark a batch as sent — only once a payment instrument has been issued and
    linked. The instrument is the settlement record for the trust liability;
    bank reconciliation later only flips it to Cleared (no extra journals)."""
    def post(self, request, pk):
        batch = get_object_or_404(RemittanceBatch, pk=pk)
        if batch.status != RemittanceBatch.Status.APPROVED:
            messages.error(request, "Approve the batch before marking it sent.")
            return redirect("remittance_batch_detail", pk=pk)
        if not batch.is_settled:
            messages.error(request, "Issue and link a payment instrument (cheque, "
                "EFT, M-Pesa, etc.) before marking this batch as sent.")
            return redirect("remittance_batch_detail", pk=pk)
        inst = batch.payment
        paid_date = inst.date_issued or _dt.date.today()
        batch.status = RemittanceBatch.Status.REMITTED
        batch.remitted_at = _tz.now()
        # keep the legacy fields in step for any old reports still reading them
        if inst.method == "CHEQUE":
            batch.cheque_no = inst.instrument_number[:30]
            batch.cheque_date = inst.date_issued
        batch.save(update_fields=["status", "remitted_at", "cheque_no", "cheque_date"])
        batch.expenses.update(status=Expense.Status.PAID, paid_date=paid_date,
                              voucher_no=inst.instrument_number[:30])
        _repost_to_ledger(batch.expenses.all())
        messages.success(request, f"Batch {batch.batch_number} marked sent, settled by "
                                  f"{inst.get_method_display()} {inst.instrument_number}.")
        return redirect("remittance_batch_detail", pk=pk)

class RemittanceBatchIssuePaymentView(TreasurerRequiredMixin, View):
    """Issue a payment instrument that settles this remittance batch and link it.
    Posts no journal entries — the batch's remittance expenses already account
    for the liability; this only records how it is being paid."""
    def post(self, request, pk):
        from cashbook.models import PaymentInstrument
        batch = get_object_or_404(RemittanceBatch, pk=pk)
        if batch.status not in (RemittanceBatch.Status.APPROVED,
                                RemittanceBatch.Status.DRAFT):
            messages.error(request, "A payment can only be issued for a draft or "
                                    "approved batch.")
            return redirect("remittance_batch_detail", pk=pk)
        method = request.POST.get("method") or "CHEQUE"
        number = (request.POST.get("instrument_number") or "").strip()[:40]
        try:
            issued = _dt.date.fromisoformat(request.POST.get("date_issued")) \
                if request.POST.get("date_issued") else _dt.date.today()
        except ValueError:
            issued = _dt.date.today()
        bank_id = request.POST.get("bank_account") or ""
        inst = PaymentInstrument(
            method=method, instrument_number=number,
            payee="Conference remittance", amount=batch.total_amount,
            date_issued=issued, status=PaymentInstrument.Status.ISSUED,
            source_kind=PaymentInstrument.SourceKind.REMITTANCE,
            remittance_batch=batch, recorded_by=request.user)
        if bank_id.isdigit():
            from statements.models import BankAccount
            inst.bank_account = BankAccount.objects.filter(pk=bank_id).first()
        inst.save()
        batch.payment = inst
        batch.save(update_fields=["payment"])
        messages.success(request, f"Issued {inst.get_method_display()} "
                                  f"{inst.instrument_number} for this remittance.")
        return redirect("remittance_batch_detail", pk=pk)

class RemittanceBatchListView(ReportAccessMixin, TemplateView):
    template_name = "reports/remittance_batches.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["batches"] = RemittanceBatch.objects.all()
        return ctx

class RemittanceCalendarView(ReportAccessMixin, TemplateView):
    """Per-year remittance calendar: each period's deadline and the reporting
    Sabbath it maps to (the most recent Saturday on/before the deadline). When a
    deadline doesn't fall on a Sabbath, the previous Sabbath is shown as the
    reporting Sabbath, and due-soon / overdue items are highlighted."""
    template_name = "reports/remittance_calendar.html"

    def get_context_data(self, **kwargs):
        from cashbook.models import RemittanceDeadline, RemittanceBatch
        import calendar as _cal
        ctx = super().get_context_data(**kwargs)
        today = _dt.date.today()
        try:
            year = int(self.request.GET.get("year", today.year))
        except (TypeError, ValueError):
            year = today.year
        deadlines = list(RemittanceDeadline.objects.filter(year=year))

        # auto-mark a period as remitted when a remitted batch covers it. A batch
        # covers a month if its period overlaps that month, or (if it has no
        # period set) if it was remitted within the month. This keeps the
        # calendar honest without the treasurer ticking each box by hand.
        remitted_batches = list(RemittanceBatch.objects.filter(
            status=RemittanceBatch.Status.REMITTED))
        auto_marked = 0
        for d in deadlines:
            if d.remitted:
                continue
            m_start = _dt.date(year, d.period_month, 1)
            m_end = _dt.date(year, d.period_month,
                             _cal.monthrange(year, d.period_month)[1])
            covered = False
            for b in remitted_batches:
                ps, pe = b.period_start, b.period_end
                if ps and pe:
                    if ps <= m_end and pe >= m_start:
                        covered = True; break
                elif b.remitted_at and m_start <= b.remitted_at.date() <= m_end:
                    covered = True; break
            if covered:
                d.remitted = True
                d.save(update_fields=["remitted"])
                auto_marked += 1
        if auto_marked:
            messages.info(self.request,
                f"{auto_marked} period(s) marked remitted automatically from "
                f"completed remittance batches.")

        ctx["year"] = year
        ctx["prev_year"] = year - 1
        ctx["next_year"] = year + 1
        ctx["deadlines"] = deadlines
        ctx["has_any"] = bool(deadlines)
        ctx["due_soon"] = [d for d in deadlines if d.is_due_soon]
        ctx["overdue"] = [d for d in deadlines if d.is_overdue]
        return ctx

class RemittanceCalendarGenerateView(TreasurerRequiredMixin, View):
    """Auto-generate a year's monthly remittance deadlines. The default deadline
    is the 1st of the following month (adjustable afterwards). Existing periods
    are left untouched."""

    def post(self, request):
        from cashbook.models import RemittanceDeadline
        import calendar
        today = _dt.date.today()
        try:
            year = int(request.POST.get("year", today.year))
        except (TypeError, ValueError):
            year = today.year
        try:
            due_day = int(request.POST.get("due_day", 1))
        except (TypeError, ValueError):
            due_day = 1
        due_day = min(max(due_day, 1), 28)
        created = 0
        for m in range(1, 13):
            # deadline falls in the FOLLOWING month by default
            dyear, dmonth = (year + 1, 1) if m == 12 else (year, m + 1)
            _, last_day = calendar.monthrange(dyear, dmonth)
            deadline = _dt.date(dyear, dmonth, min(due_day, last_day))
            _, was_created = RemittanceDeadline.objects.get_or_create(
                year=year, period_month=m,
                defaults={"deadline": deadline,
                          "label": f"{calendar.month_name[m]} remittance"})
            if was_created:
                created += 1
        messages.success(request, f"Generated {created} remittance deadline(s) for "
                                  f"{year}. You can adjust individual dates below.")
        return redirect(f"{reverse('remittance_calendar')}?year={year}")

class RemittanceDeadlineUpdateView(TreasurerRequiredMixin, View):
    """Edit one deadline's date/label/notes, or toggle its remitted flag."""

    def post(self, request, pk):
        from cashbook.models import RemittanceDeadline
        d = get_object_or_404(RemittanceDeadline, pk=pk)
        if request.POST.get("toggle_remitted"):
            d.remitted = not d.remitted
            d.save(update_fields=["remitted"])
            messages.success(request, f"{d.get_period_display()} marked "
                                      f"{'remitted' if d.remitted else 'not remitted'}.")
            return redirect(f"{reverse('remittance_calendar')}?year={d.year}")
        new_date = request.POST.get("deadline")
        if new_date:
            try:
                d.deadline = _dt.date.fromisoformat(new_date)
            except ValueError:
                messages.error(request, "Invalid date.")
                return redirect(f"{reverse('remittance_calendar')}?year={d.year}")
        d.label = (request.POST.get("label") or d.label)[:60]
        d.notes = (request.POST.get("notes") or "")[:200]
        d.save()
        messages.success(request, f"Updated {d.get_period_display()} deadline.")
        return redirect(f"{reverse('remittance_calendar')}?year={d.year}")
