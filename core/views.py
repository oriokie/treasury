from decimal import Decimal

from django.db.models import Sum
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from cashbook.models import Expense
from departments.models import Department
from giving.models import Transaction
from members.models import Member, PossibleDuplicate
from statements.models import StatementImport
from reports.services import balances
from .utils import parse_period


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def dispatch(self, request, *args, **kwargs):
        # a departmental leader has no business on the full office dashboard —
        # send them to their own scoped view
        from core.roles import is_leader
        if is_leader(request.user):
            from django.shortcuts import redirect
            return redirect("leader_dashboard")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end = parse_period(self.request)

        rows = balances.department_summary(start, end)
        ctx["start"], ctx["end"] = start, end
        ctx["totals"] = balances.totals(rows)
        ctx["trust_rows"] = balances.trust_summary(start, end)
        ctx["trust_to_remit"] = sum((r["to_remit"] for r in ctx["trust_rows"]), 0)
        ctx["local_rows"] = [r for r in rows if not r["is_trust"]]
        ctx["local_totals"] = balances.totals(ctx["local_rows"])
        from core.models import SiteConfig
        ctx["field_name"] = SiteConfig.get().field_name or "conference"
        ctx["by_group"] = balances.giving_by_group(start, end)
        ctx["by_channel"] = balances.income_by_channel(start, end)
        # Item 4: a clearer "how giving arrives" card — channel mix with shares
        from decimal import Decimal as _D
        _ch_labels = {"BANK": "Bank / M-Pesa", "CASH": "Cash", "ENVELOPE": "Envelopes"}
        _ch_total = sum((r["total"] or _D(0)) for r in ctx["by_channel"]) or _D(0)
        ctx["channel_mix"] = sorted([{
            "label": _ch_labels.get(r["channel"], r["channel"] or "Other"),
            "total": r["total"] or _D(0), "count": r["count"],
            "pct": int(round((r["total"] or _D(0)) / _ch_total * 100)) if _ch_total else 0,
        } for r in ctx["by_channel"]], key=lambda x: x["total"], reverse=True)
        ctx["channel_total"] = _ch_total
        ctx["tithe"] = balances.tithe_total(start, end)

        # --- extra dashboard insight data (item 6) ---
        import json as _json
        from decimal import Decimal
        from django.db.models import Sum as _Sum
        from django.db.models.functions import ExtractMonth as _ExM
        # income by channel (doughnut)
        _chan = {c["channel"]: float(c["total"] or 0) for c in ctx["by_channel"]}
        ctx["channel_json"] = _json.dumps([
            {"label": "Bank", "value": _chan.get("BANK", 0)},
            {"label": "Envelope", "value": _chan.get("ENVELOPE", 0)},
            {"label": "Cash", "value": _chan.get("CASH", 0)},
        ])
        # monthly receipts vs expenses within the selected period (bars)
        MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        mrec = {r["m"]: float(r["t"] or 0) for r in
                Transaction.objects.confirmed_credits().filter(
                    date__gte=start, date__lte=end, excluded_from_income=False)
                .annotate(m=_ExM("date")).values("m").annotate(t=_Sum("amount"))}
        mexp = {r["m"]: float(r["t"] or 0) for r in
                Expense.objects.filter(status__in=[Expense.Status.APPROVED,
                    Expense.Status.PAID], date__gte=start, date__lte=end)
                .exclude(category=Expense.Category.REMITTANCE)
                .annotate(m=_ExM("date")).values("m").annotate(t=_Sum("amount"))}
        active_m = sorted(set(mrec) | set(mexp))
        ctx["monthly_json"] = _json.dumps({
            "labels": [MN[m - 1] for m in active_m],
            "receipts": [mrec.get(m, 0) for m in active_m],
            "expenses": [mexp.get(m, 0) for m in active_m]})
        ctx["has_monthly"] = len(active_m) >= 2
        # top funds by receipts (horizontal bars)
        _top = sorted(ctx["local_rows"], key=lambda r: r["receipts"], reverse=True)[:8]
        ctx["topfunds_json"] = _json.dumps({
            "labels": [r["department"].name[:22] for r in _top],
            "values": [float(r["receipts"]) for r in _top]})
        ctx["has_topfunds"] = bool(_top)
        # trust outstanding total for the KPI strip
        ctx["trust_outstanding"] = sum((r["to_remit"] for r in ctx["trust_rows"]), Decimal(0))

        ctx["queue_count"] = Transaction.objects.filter(
            allocation_status=Transaction.Status.REVIEW).count()
        ctx["pending_expenses"] = Expense.objects.filter(
            status=Expense.Status.PENDING).count()
        ctx["dup_count"] = PossibleDuplicate.objects.filter(resolved=False).count()
        ctx["member_count"] = Member.objects.filter(active=True).count()
        ctx["fund_count"] = Department.objects.filter(active=True).count()
        ctx["recent_imports"] = StatementImport.objects.all()[:5]

        # remittance deadline alerts (item 5): surface overdue / due-soon trust
        # remittances so the treasurer is reminded ahead of the deadline.
        from cashbook.models import RemittanceDeadline
        _today = __import__("datetime").date.today()
        _rd = RemittanceDeadline.objects.filter(remitted=False,
                                                deadline__gte=_today - __import__("datetime").timedelta(days=60))
        ctx["remit_overdue"] = [d for d in _rd if d.is_overdue]
        ctx["remit_due_soon"] = [d for d in _rd if d.is_due_soon]

        # --- pledge attention signals -----------------------------------------
        # Pledges are informational, but drafts awaiting approval and lapsed/
        # overdue pledges are things a treasurer should act on.
        try:
            from pledges.models import Pledge
            ctx["pledge_draft_count"] = Pledge.objects.filter(
                status=Pledge.Status.DRAFT).count()
            _open = Pledge.objects.filter(
                status__in=[Pledge.Status.ACTIVE, Pledge.Status.LAPSED]
            ).select_related("member", "campaign")
            ctx["pledge_overdue_count"] = sum(1 for p in _open if p.is_overdue)
        except Exception:
            ctx["pledge_draft_count"] = 0
            ctx["pledge_overdue_count"] = 0

        # --- consolidated "needs attention" list ------------------------------
        # One place that surfaces everything quietly rotting, each with a count,
        # tone and link. Only non-zero items appear.
        from django.urls import reverse as _rev
        attention = []
        if ctx["queue_count"]:
            attention.append({"label": "transactions need allocating",
                "count": ctx["queue_count"], "tone": "warn",
                "url": _rev("queue"), "icon": "◷"})
        if ctx["pending_expenses"]:
            attention.append({"label": "expenses awaiting approval",
                "count": ctx["pending_expenses"], "tone": "warn",
                "url": _rev("expense_list") + "?status=PENDING", "icon": "✓"})
        if ctx["pledge_draft_count"]:
            attention.append({"label": "pledges awaiting approval",
                "count": ctx["pledge_draft_count"], "tone": "warn",
                "url": _rev("pledge_list") + "?status=DRAFT", "icon": "♡"})
        if ctx["remit_overdue"]:
            attention.append({"label": "trust remittance(s) overdue",
                "count": len(ctx["remit_overdue"]), "tone": "danger",
                "url": _rev("remittance_dashboard"), "icon": "⚠"})
        elif ctx["remit_due_soon"]:
            attention.append({"label": "remittance(s) due soon",
                "count": len(ctx["remit_due_soon"]), "tone": "warn",
                "url": _rev("remittance_dashboard"), "icon": "⏲"})
        if ctx["pledge_overdue_count"]:
            attention.append({"label": "pledges overdue (past end date)",
                "count": ctx["pledge_overdue_count"], "tone": "muted",
                "url": _rev("pledge_list") + "?status=LAPSED", "icon": "♡"})
        if ctx["dup_count"]:
            attention.append({"label": "possible duplicate(s) to review",
                "count": ctx["dup_count"], "tone": "muted",
                "url": _rev("member_duplicates"), "icon": "⧉"})
        ctx["attention"] = attention

        # multi-year trend: prior years (reference) + the current year so far
        import json, datetime as _dt
        from decimal import Decimal
        from django.db.models import Sum
        from core.models import HistoricalYear
        trend = [{"year": h.year, "collection": float(h.collection),
                  "trust": float(h.trust_fund), "expenditure": float(h.expenditure)}
                 for h in HistoricalYear.objects.all()]
        cur_year = _dt.date.today().year
        if not any(t["year"] == cur_year for t in trend):
            ys, ye = _dt.date(cur_year, 1, 1), _dt.date(cur_year, 12, 31)
            cc = (Transaction.objects.confirmed_credits().filter(
                  date__gte=ys, date__lte=ye, excluded_from_income=False)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
            ce = (Expense.objects.filter(status__in=[Expense.Status.APPROVED,
                  Expense.Status.PAID], date__gte=ys, date__lte=ye)
                  .exclude(category=Expense.Category.REMITTANCE)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
            ct = (Transaction.objects.confirmed_credits().filter(
                  date__gte=ys, date__lte=ye, department__fund_type="TRUST",
                  excluded_from_income=False)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
            if cc or ce or ct:
                trend.append({"year": cur_year, "collection": float(cc),
                              "trust": float(ct), "expenditure": float(ce)})
        trend.sort(key=lambda t: t["year"])
        ctx["trend_json"] = json.dumps(trend)
        ctx["has_trend"] = len(trend) >= 2
        return ctx


# ---- System configuration ----
from django.contrib import messages
from django.http import JsonResponse
from django.urls import reverse
from django.shortcuts import redirect, render
from django.views import View

from core.permissions import TreasurerRequiredMixin, ReadAccessMixin, DataEntryRequiredMixin
from core.models import SiteConfig, SmsLog
from core.forms import SiteConfigForm


class SettingsView(TreasurerRequiredMixin, View):
    template_name = "settings.html"

    def get(self, request):
        cfg = SiteConfig.get()
        from core.models import TelegramProfile
        from django.contrib.auth.models import User
        from core.roles import role_label
        mine = TelegramProfile.objects.filter(user=request.user).first()
        tg_users = []
        for u in User.objects.filter(is_active=True).order_by("username"):
            prof = getattr(u, "telegram_profile", None)
            tg_users.append({"name": u.get_full_name() or u.username,
                             "role": role_label(u),
                             "has_pin": bool(prof and prof.pin)})
        return render(request, self.template_name, {
            "form": SiteConfigForm(instance=cfg),
            "recent_sms": SmsLog.objects.all()[:10],
            "cbs_webhook_url": request.build_absolute_uri(reverse("cbs_webhook")),
            "my_telegram_pin": mine.pin if mine else "",
            "telegram_users": tg_users,
        })

    def post(self, request):
        cfg = SiteConfig.get()
        if "send_test" in request.POST:
            from core.services.sms import send_sms
            to = request.POST.get("test_to", "").strip()
            log = send_sms(to, "Test message from the church treasury system.", cfg)
            messages.info(request, f"Test SMS: {log.get_status_display()} — {log.response[:120]}")
            return redirect("settings")
        if "test_email" in request.POST:
            from core.services.email import test_email
            form = SiteConfigForm(request.POST, instance=cfg)
            if form.is_valid():
                cfg = form.save()
            to = (request.POST.get("email_test_to") or cfg.email_from or "").strip()
            ok, detail = test_email(to, cfg)
            (messages.success if ok else messages.error)(
                request, f"Email test {'sent' if ok else 'failed'} — {detail}")
            return redirect("settings")
        if "test_llm" in request.POST:
            from core.services.assistant import test_llm
            form = SiteConfigForm(request.POST, instance=cfg)
            if form.is_valid():
                cfg = form.save()
            else:
                messages.error(request, "Fix the highlighted settings before testing.")
                return render(request, self.template_name,
                              {"form": form, "recent_sms": SmsLog.objects.all()[:10]})
            ok, detail = test_llm(cfg)
            if ok:
                messages.success(request, f"Assistant LLM is working — the model replied: "
                                          f"\u201c{detail}\u201d")
            else:
                messages.error(request, f"Assistant LLM test failed — {detail}")
            return redirect("settings")
        form = SiteConfigForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            messages.success(request, "Settings saved.")
            return redirect("settings")
        return render(request, self.template_name, {
            "form": form, "recent_sms": SmsLog.objects.all()[:10]})


class MemberSearchView(DataEntryRequiredMixin, View):
    """JSON typeahead for contributor name fields (up to 5 suggestions)."""

    def get(self, request):
        from django.db.models import Q
        from members.models import Member
        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})
        qs = (Member.objects.filter(active=True)
              .filter(Q(name__icontains=q) | Q(phone__icontains=q))
              .order_by("name")[:5])
        results = [{"id": m.id, "name": m.name, "phone": m.phone or "",
                    "type": m.get_member_type_display() if m.member_type else ""}
                   for m in qs]
        return JsonResponse({"results": results})


class NextReceiptView(DataEntryRequiredMixin, View):
    """Return the next sequential envelope receipt number."""

    def get(self, request):
        from envelopes.models import Envelope
        nums = []
        for r in Envelope.objects.values_list("receipt_no", flat=True):
            digits = "".join(ch for ch in str(r) if ch.isdigit())
            if digits:
                nums.append(int(digits))
        nxt = (max(nums) + 1) if nums else 1
        return JsonResponse({"next": str(nxt)})


class DepartmentBalanceView(DataEntryRequiredMixin, View):
    """JSON: a department's available balance (opening + receipts − approved/paid
    expenses), for the expense form to show on selection."""

    def get(self, request):
        from decimal import Decimal
        from django.db.models import Sum
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        try:
            dept = Department.objects.get(pk=request.GET.get("id"))
        except (Department.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"ok": False})
        credits = (Transaction.objects.filter(
            department=dept, direction=Transaction.Direction.CREDIT, confirmed=True)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        spent = (Expense.objects.exclude(
            category=Expense.Category.REMITTANCE).filter(
            department=dept,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        from reports.services import balances as _bal
        tin = _bal.transfers_in_by_department().get(dept.id, Decimal(0))
        tout = _bal.transfers_out_by_department().get(dept.id, Decimal(0))
        bal = ((dept.opening_balance or Decimal(0)) + credits + tin - tout - spent)
        return JsonResponse({
            "ok": True, "name": dept.name,
            "fund_type": "Trust" if dept.is_trust else "Local",
            "opening": float(dept.opening_balance or 0),
            "receipts": float(credits), "spent": float(spent),
            "transfers_in": float(tin), "transfers_out": float(tout),
            "balance": float(bal)})


class AssistantView(ReadAccessMixin, View):
    """Chat-style assistant page."""
    def get(self, request):
        from .services.assistant import SUGGESTIONS, SUGGESTION_GROUPS
        from core.models import SiteConfig
        return render(request, "assistant.html", {
            "suggestions": SUGGESTIONS,
            "suggestion_groups": SUGGESTION_GROUPS,
            "llm_on": SiteConfig.get().llm_enabled})


class AssistantAskView(ReadAccessMixin, View):
    """Answer one question as JSON."""
    def post(self, request):
        import json as _json
        from .services import assistant
        try:
            q = _json.loads(request.body.decode() or "{}").get("q", "")
        except Exception:
            q = request.POST.get("q", "")
        try:
            data = assistant.answer(q, request.user)
        except Exception as exc:
            data = {"text": f"Sorry — I hit an error answering that ({type(exc).__name__})."}
        return JsonResponse(data)


class FundSearchView(ReadAccessMixin, View):
    """Typeahead for fund pickers. scope=income adds split funds as single
    options; scope=expense restricts to expense-eligible funds."""
    def get(self, request):
        from departments.models import expense_departments, income_departments
        q = (request.GET.get("q") or "").strip().lower()
        scope = request.GET.get("scope", "income")
        results = []
        if scope == "expense":
            depts = expense_departments()
        else:
            depts = income_departments()
        for d in depts:
            if not q or q in d.name.lower():
                tag = "Trust" if d.is_trust else "Local"
                results.append({"key": f"d:{d.id}", "id": d.id,
                                "label": d.name, "tag": tag})
        if scope == "income":
            from giving.models import SplitFund
            for s in SplitFund.objects.filter(active=True):
                if not q or q in s.name.lower():
                    results.append({"key": f"s:{s.id}", "id": s.id,
                                    "label": f"{s.name} (split)", "tag": "Split"})
        results.sort(key=lambda r: r["label"].lower())
        return JsonResponse({"results": results[:20]})


class ControlsView(TreasurerRequiredMixin, View):
    """Treasury controls: lock/unlock accounting periods and review possible
    duplicate entries."""
    template_name = "controls.html"

    def get(self, request):
        import calendar
        import datetime as dt
        from decimal import Decimal
        from .models import PeriodLock, YearEndClose
        from reports.services import balances
        year = int(request.GET.get("year") or dt.date.today().year)
        locks = {l.month: l for l in PeriodLock.objects.filter(year=year)}
        months = [{"num": m, "name": calendar.month_name[m], "lock": locks.get(m)}
                  for m in range(1, 13)]
        # year-end close: preview the balances that would carry forward
        close_year = int(request.GET.get("close_year") or (dt.date.today().year - 1))
        cf_rows = balances.department_summary(dt.date(close_year, 1, 1),
                                              dt.date(close_year, 12, 31))
        cf_total = sum((r["closing"] for r in cf_rows), Decimal(0))
        return render(request, self.template_name, {
            "year": year, "months": months,
            "years": range(dt.date.today().year + 1, dt.date.today().year - 5, -1),
            "dup_expenses": _duplicate_expenses(),
            "dup_offerings": _duplicate_offerings(),
            "close_year": close_year,
            "cf_rows": cf_rows, "cf_total": cf_total,
            "closes": YearEndClose.objects.all(),
            "is_closed": (lambda c: bool(c and c.is_effective))(
                YearEndClose.objects.filter(year=close_year).first()),
            "pending_close": (lambda c: c if (c and not c.is_effective) else None)(
                YearEndClose.objects.filter(year=close_year).first()),
        })

    def post(self, request):
        import datetime as dt
        from .models import PeriodLock
        action = request.POST.get("action")
        if action in ("close_year", "reopen_year", "confirm_close"):
            {"close_year": self._close_year, "reopen_year": self._reopen_year,
             "confirm_close": self._confirm_close}[action](request)
            return redirect(f"{reverse('controls')}?close_year={request.POST.get('close_year')}")
        year = int(request.POST.get("year"))
        month = int(request.POST.get("month"))
        if action == "lock":
            PeriodLock.objects.get_or_create(
                year=year, month=month,
                defaults=dict(locked_by=request.user,
                              note=request.POST.get("note", "")[:200]))
            messages.success(request, f"Locked {dt.date(year, month, 1):%B %Y}.")
        elif action == "unlock":
            if not request.user.is_superuser:
                messages.error(request, "Only an administrator can unlock a period.")
            else:
                PeriodLock.objects.filter(year=year, month=month).delete()
                messages.success(request, f"Unlocked {dt.date(year, month, 1):%B %Y}.")
        return redirect(f"{reverse('controls')}?year={year}")

    def _close_year(self, request):
        import datetime as dt
        from decimal import Decimal
        from .models import YearEndClose
        from core.models import SiteConfig
        from reports.services import balances
        y = int(request.POST.get("close_year"))
        existing = YearEndClose.objects.filter(year=y).first()
        if existing and existing.is_effective:
            messages.error(request, f"{y} is already closed.")
            return
        if existing:        # pending confirmation already exists
            messages.info(request, f"{y} is awaiting confirmation by a second treasurer.")
            return
        rows = balances.department_summary(dt.date(y, 1, 1), dt.date(y, 12, 31))
        total = sum((r["closing"] for r in rows), Decimal(0))
        close = YearEndClose.objects.create(year=y, closed_by=request.user,
                                            total_carried=total,
                                            note=request.POST.get("note", "")[:200])
        if SiteConfig.get().require_dual_yearend:
            messages.warning(request, f"Year {y} close initiated. A second treasurer must "
                                      f"confirm before balances carry forward and months lock.")
            return
        self._finalize_close(close, request)

    def _confirm_close(self, request):
        from django.utils import timezone
        from .models import YearEndClose
        y = int(request.POST.get("close_year"))
        close = YearEndClose.objects.filter(year=y).first()
        if not close:
            messages.error(request, f"No pending close for {y}.")
            return
        if close.is_effective:
            messages.info(request, f"{y} is already closed.")
            return
        if close.closed_by_id == request.user.id:
            messages.error(request, "A second, different treasurer must confirm the close.")
            return
        close.confirmed_by = request.user
        close.confirmed_at = timezone.now()
        close.save(update_fields=["confirmed_by", "confirmed_at"])
        self._finalize_close(close, request)

    def _finalize_close(self, close, request):
        import datetime as dt
        from .models import PeriodLock, FundCarryForward
        from reports.services import balances
        y = close.year
        rows = balances.department_summary(dt.date(y, 1, 1), dt.date(y, 12, 31))
        FundCarryForward.objects.filter(close=close).delete()
        FundCarryForward.objects.bulk_create([
            FundCarryForward(close=close, department=r["department"],
                             closing_balance=r["closing"]) for r in rows])
        for m in range(1, 13):
            PeriodLock.objects.get_or_create(
                year=y, month=m, defaults=dict(locked_by=request.user,
                                               note=f"Year {y} closed"))
        # capture the year's headline totals for historical comparison
        self._snapshot_historical_year(y)
        messages.success(
            request, f"Year {y} closed. {len(rows)} fund balance(s) carried forward "
                     f"to {y + 1} and the year's months were locked.")

    def _snapshot_historical_year(self, y):
        """On year close, record (or refresh) the year's collection / trust /
        expenditure totals as a HistoricalYear, and a per-month breakdown as
        HistoricalMonth rows, so closed years feed the multi-year comparison
        automatically going forward."""
        import datetime as dt
        from decimal import Decimal
        from django.db.models import Sum
        from django.db.models.functions import ExtractMonth
        from giving.models import Transaction
        from cashbook.models import Expense
        from .models import HistoricalYear, HistoricalMonth
        s, e = dt.date(y, 1, 1), dt.date(y, 12, 31)
        coll = (Transaction.objects.confirmed_credits().filter(
                date__gte=s, date__lte=e, excluded_from_income=False)
                .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        trust = (Transaction.objects.confirmed_credits().filter(
                 date__gte=s, date__lte=e, department__fund_type="TRUST",
                 excluded_from_income=False)
                 .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        exp = (Expense.objects.filter(status__in=[Expense.Status.APPROVED,
               Expense.Status.PAID], date__gte=s, date__lte=e)
               .exclude(category=Expense.Category.REMITTANCE)
               .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        HistoricalYear.objects.update_or_create(
            year=y, defaults=dict(collection=coll, trust_fund=trust,
                                  expenditure=exp, note=f"Captured at year-end close of {y}"))
        # per-month detail for seasonality
        def by_month(qs):
            return {r["m"]: r["t"] for r in qs.annotate(m=ExtractMonth("date"))
                    .values("m").annotate(t=Sum("amount"))}
        mc = by_month(Transaction.objects.confirmed_credits().filter(
            date__gte=s, date__lte=e, excluded_from_income=False))
        mt = by_month(Transaction.objects.confirmed_credits().filter(
            date__gte=s, date__lte=e, department__fund_type="TRUST",
            excluded_from_income=False))
        me = by_month(Expense.objects.exclude(
            category=Expense.Category.REMITTANCE).filter(status__in=[Expense.Status.APPROVED,
            Expense.Status.PAID], date__gte=s, date__lte=e))
        for m in range(1, 13):
            if mc.get(m) or mt.get(m) or me.get(m):
                HistoricalMonth.objects.update_or_create(
                    year=y, month=m, defaults=dict(
                        collection=mc.get(m, 0) or 0, trust_fund=mt.get(m, 0) or 0,
                        expenditure=me.get(m, 0) or 0))

    def _reopen_year(self, request):
        from .models import PeriodLock, YearEndClose
        if not request.user.is_superuser:
            messages.error(request, "Only an administrator can re-open a closed year.")
            return
        y = int(request.POST.get("close_year"))
        YearEndClose.objects.filter(year=y).delete()
        PeriodLock.objects.filter(year=y).delete()
        messages.success(request, f"Year {y} re-opened and unlocked.")


def _duplicate_expenses():
    """Likely duplicate expenses: same Sabbath week + same person paid (claimant)
    + same amount + same fund + same description. Grouping by the Sabbath (rather
    than the whole month) keeps the flag tight — only re-entries within the same
    counting week show up. Bank / M-Pesa charges are excluded — they're naturally
    identical across many payments and would flood the list with false positives."""
    from django.db.models import Count, Min, Max
    from cashbook.models import Expense
    from core.models import service_sabbath_for
    # group in Python by the service Sabbath of each expense's date, since the
    # Sabbath is derived (not a stored column on Expense)
    rows = list(Expense.objects.exclude(category=Expense.Category.BANK_CHARGE)
                .values("id", "date", "amount", "department__name",
                        "description", "claimant"))
    # Bank / M-Pesa charges are naturally identical across many payments and would
    # flood the list with false positives. We already drop the BANK_CHARGE
    # category above; also drop anything whose description reads as a transaction
    # charge, in case a charge was recorded under a different category.
    def _is_charge(desc):
        d = (desc or "").lower()
        return ("transaction charge" in d or "m-pesa charge" in d
                or "mpesa charge" in d or "bank charge" in d
                or "withdrawal charge" in d or "paybill charge" in d)
    rows = [r for r in rows if not _is_charge(r["description"])]
    buckets = {}
    for r in rows:
        sab = service_sabbath_for(r["date"])
        key = (sab, r["amount"], r["department__name"], r["description"],
               r["claimant"])
        buckets.setdefault(key, []).append(r)
    out = []
    for key, grp in buckets.items():
        if len(grp) < 2:
            continue
        sab, amount, fund, desc, claimant = key
        dates = sorted(r["date"] for r in grp)
        out.append({"date": dates[-1], "first": dates[0], "amount": amount,
                    "fund": fund, "description": desc,
                    "claimant": claimant or "—", "count": len(grp),
                    "sabbath": sab})
    out.sort(key=lambda c: c["date"], reverse=True)
    return out[:50]


def _duplicate_offerings():
    """Likely duplicate offerings, by three signals:

    1. Same reference (M-Pesa receipt / paybill ref) appearing on more than one
       non-excluded credit — a near-certain double import of the same payment.
    2. Same giver (order-insensitive name match) + same amount + **same channel**
       within 3 days — a probable re-entry. Requiring the same channel avoids
       flagging a giver who legitimately gave the same amount once by cash and
       once by M-Pesa.
    3. Same giver + same amount appearing more than once in the **envelopes** for
       the same Sabbath — a re-typed envelope.

    Envelope detail rows of bank giving (excluded_from_income) and reversals are
    duplicates by design and are not flagged; the 3-day window avoids flagging a
    faithful weekly giver of a fixed amount."""
    from django.db.models import Count, Min, Max
    from giving.models import Transaction
    from members.services.matching import name_key
    base = Transaction.objects.filter(
        direction=Transaction.Direction.CREDIT, is_reversal=False,
        is_reversed=False, excluded_from_income=False)

    out = []
    seen_refs = set()
    # 1) same non-empty reference on more than one credit -> strong duplicate
    ref_groups = (base.exclude(reference="")
                  .values("reference", "amount")
                  .annotate(n=Count("id"), first=Min("date"), last=Max("date"),
                            payer=Min("payer_name"))
                  .filter(n__gt=1).order_by("-last"))
    for g in ref_groups[:50]:
        seen_refs.add(g["reference"])
        out.append({"date": g["last"], "first": g["first"], "amount": g["amount"],
                    "payer": g["payer"] or "—", "count": g["n"],
                    "reference": g["reference"], "by": "reference",
                    "channel": ""})

    # 2) same giver + same amount + same CHANNEL within 3 days (skip refs caught above)
    rows = list(base.exclude(payer_name="")
                .values("date", "amount", "payer_name", "reference", "channel"))
    byk = {}
    for r in rows:
        if r["reference"] and r["reference"] in seen_refs:
            continue
        nk = name_key(r["payer_name"])
        if nk:
            byk.setdefault((nk, r["amount"], r["channel"]), []).append(r)
    for (nk, amt, channel), grp in byk.items():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda r: r["date"])
        cluster = [grp[0]]
        for prev, cur in zip(grp, grp[1:]):
            if (cur["date"] - prev["date"]).days <= 3:
                cluster.append(cur)
            else:
                if len(cluster) > 1:
                    out.append(_off_cluster(cluster))
                cluster = [cur]
        if len(cluster) > 1:
            out.append(_off_cluster(cluster))

    # 3) duplicate envelopes: same giver + same amount on the same Sabbath
    out.extend(_duplicate_envelopes())

    # 4) NEAR-match givers: same amount + same channel within 3 days where the
    #    names are *almost* equal — catches a manual receipt typed with a slightly
    #    misspelt name (e.g. "Jon Mwangi" vs "John Mwangi") that the exact
    #    order-insensitive key in (2) would miss.
    out.extend(_fuzzy_name_duplicates(base, seen_refs))

    # de-duplicate clusters that more than one signal produced (same payer+amount+date)
    deduped, sig_seen = [], set()
    for c in out:
        sig = (c.get("payer"), c.get("amount"), c.get("first"), c.get("last"),
               c.get("by"))
        if sig in sig_seen:
            continue
        sig_seen.add(sig)
        deduped.append(c)
    # sort by payer (the user reviews these name-by-name), then most recent first
    deduped.sort(key=lambda c: ((c.get("payer") or "~").upper(), c["date"]),
                 reverse=False)
    return deduped[:80]


def _fuzzy_name_duplicates(base, seen_refs, threshold=0.86):
    """Clusters of credits with the SAME amount + channel within 3 days whose
    payer names are near-matches (above a similarity threshold) but not identical.
    Surfaces probable re-entries where a name was misspelt on a manual receipt."""
    from difflib import SequenceMatcher
    from members.services.matching import name_key
    rows = list(base.exclude(payer_name="")
                .values("date", "amount", "payer_name", "reference", "channel"))
    # bucket by amount + channel so we only compare plausibly-related gifts
    buckets = {}
    for r in rows:
        if r["reference"] and r["reference"] in seen_refs:
            continue
        if not (r["payer_name"] or "").strip():
            continue
        buckets.setdefault((r["amount"], r["channel"]), []).append(r)

    def close(a, b):
        ka, kb = name_key(a), name_key(b)
        if not ka or not kb or ka == kb:
            return False                      # identical handled by exact pass (2)
        return SequenceMatcher(None, ka, kb).ratio() >= threshold

    out = []
    for (amt, channel), grp in buckets.items():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda r: r["date"])
        used = [False] * len(grp)
        for i in range(len(grp)):
            if used[i]:
                continue
            cluster = [grp[i]]
            for j in range(i + 1, len(grp)):
                if used[j]:
                    continue
                if abs((grp[j]["date"] - grp[i]["date"]).days) <= 3 \
                        and close(grp[i]["payer_name"], grp[j]["payer_name"]):
                    cluster.append(grp[j]); used[j] = True
            if len(cluster) > 1:
                used[i] = True
                names = sorted({c["payer_name"] for c in cluster})
                dates = sorted(c["date"] for c in cluster)
                out.append({"date": dates[-1], "first": dates[0], "amount": amt,
                            "payer": " / ".join(names), "count": len(cluster),
                            "reference": "", "by": "near-match name",
                            "channel": channel})
    return out


def _duplicate_envelopes():
    """Same giver + same total on the same Sabbath, recorded as more than one
    envelope — a probable re-typed envelope."""
    from envelopes.models import Envelope
    from members.services.matching import name_key
    rows = list(Envelope.objects.values("date", "total", "contributor_name",
                                         "receipt_no"))
    byk = {}
    for r in rows:
        nk = name_key(r["contributor_name"])
        if nk and r["total"]:
            byk.setdefault((nk, r["total"], r["date"]), []).append(r)
    out = []
    for (nk, amt, date), grp in byk.items():
        if len(grp) < 2:
            continue
        out.append({"date": date, "first": date, "amount": amt,
                    "payer": grp[0]["contributor_name"] or "—",
                    "count": len(grp), "by": "envelope",
                    "reference": ", ".join(r["receipt_no"] for r in grp if r["receipt_no"])[:40] or "—",
                    "channel": "envelope"})
    return out


def _off_cluster(c):
    return {"date": c[-1]["date"], "first": c[0]["date"], "amount": c[0]["amount"],
            "payer": c[0]["payer_name"], "count": len(c),
            "reference": next((r["reference"] for r in c if r["reference"]), "—"),
            "by": "name+amount", "channel": c[0].get("channel", "")}


class ExecutiveDashboardView(ReadAccessMixin, View):
    """Executive overview: KPI cards, Chart.js trends, and health/anomaly alerts."""
    template_name = "executive.html"

    @staticmethod
    def _key(a):
        from django.utils.text import slugify
        return a.get("key") or slugify(a.get("title", ""))[:60]

    def get(self, request):
        import json
        from .services import dashboard, health
        from core.models import SiteConfig
        ch = dashboard.charts()
        dismissed = set(request.session.get("dismissed_alerts", []))
        alerts = []
        for a in health.anomalies():
            a["key"] = self._key(a)
            if a["key"] not in dismissed:
                alerts.append(a)
        return render(request, self.template_name, {
            "cards": dashboard.cards(),
            "insights": dashboard.insights(),
            "kpis": health.kpis(),
            "alerts": alerts,
            "dismissed_count": len(dismissed),
            "charts_json": json.dumps(ch),
            "ai_enabled": SiteConfig.get().llm_enabled,
        })

    def post(self, request):
        """Dismiss an alert (no action needed) or restore all dismissed alerts."""
        dismissed = set(request.session.get("dismissed_alerts", []))
        if request.POST.get("restore"):
            dismissed = set()
            messages.info(request, "Dismissed alerts restored.")
        else:
            key = request.POST.get("dismiss")
            if key:
                dismissed.add(key)
        request.session["dismissed_alerts"] = list(dismissed)
        return redirect("executive")


class ExecutiveInsightsView(ReadAccessMixin, View):
    """AJAX: AI-generated executive briefing (costs one LLM call, so on demand)."""
    def get(self, request):
        from django.http import JsonResponse
        from core.services.assistant import executive_insights
        text, err = executive_insights()
        if err:
            low = err.lower()
            if "switched off" in low or "disabled" in low:
                witty = ("🙈 The AI assistant is taking a Sabbath rest — it's switched "
                         "off. Flip it on in Settings → Assistant and I'll crunch the "
                         "numbers for you.")
            elif "key" in low or "401" in low or "403" in low or "auth" in low:
                witty = ("🔑 I'd love to share my wisdom, but my API key isn't opening "
                         "any doors. Pop a valid key into Settings → Assistant and "
                         "I'll be back with insights — and possibly a stewardship pun.")
            elif "model" in low or "404" in low or "non-json" in low or "base url" in low:
                witty = ("🤔 I tried to think deep thoughts, but the assistant's model "
                         "or address looks off. Check the provider, model and base URL "
                         "in Settings → Assistant — then ask me again.")
            else:
                witty = ("😅 My crystal ball is cloudy right now (" + err[:80] + "). "
                         "The figures on this page are solid, though — give it another "
                         "go in a moment.")
            return JsonResponse({"ok": False, "error": witty})
        return JsonResponse({"ok": True, "insights": text})


class NotificationListView(LoginRequiredMixin, View):
    template_name = "notifications.html"

    def get(self, request):
        from django.db.models import Q
        from core.models import Notification
        qs = Notification.objects.filter(
            Q(recipient=request.user) | Q(recipient__isnull=True)).order_by("-created_at")[:100]
        return render(request, self.template_name, {"notifications": qs})

    def post(self, request):
        from django.db.models import Q
        from core.models import Notification
        Notification.objects.filter(
            Q(recipient=request.user) | Q(recipient__isnull=True), read=False).update(read=True)
        messages.success(request, "Notifications marked as read.")
        return redirect("notifications")


class TelegramSetPinView(ReadAccessMixin, View):
    """Each signed-in user sets/clears their own Telegram PIN."""
    def post(self, request):
        from core.models import TelegramProfile
        pin = (request.POST.get("pin") or "").strip()
        if pin and (not pin.isdigit() or not (4 <= len(pin) <= 8)):
            messages.error(request, "PIN must be 4–8 digits.")
            return redirect("settings")
        # personal PINs must be unique so the bot can identify the user
        if pin:
            clash = TelegramProfile.user_for_pin(pin)
            if clash and clash.id != request.user.id:
                messages.error(request, "That PIN is already taken — choose another.")
                return redirect("settings")
        prof, _ = TelegramProfile.objects.get_or_create(user=request.user)
        prof.pin = pin
        prof.save()
        messages.success(request, "Your Telegram PIN was updated."
                         if pin else "Your personal Telegram PIN was cleared.")
        return redirect("settings")


class BackupView(TreasurerRequiredMixin, View):
    """Download a full database backup (restorable snapshot)."""
    def get(self, request):
        from core.services.backup import database_backup_response
        return database_backup_response()


class DataExportView(TreasurerRequiredMixin, View):
    """Download all operational data as a multi-sheet Excel workbook."""
    def get(self, request):
        from core.services.backup import full_excel_export_response
        return full_excel_export_response()


class RestoreView(TreasurerRequiredMixin, View):
    """Restore the database from an uploaded backup. Destructive — requires an
    explicit typed confirmation and takes a safety backup of current data first."""
    def post(self, request):
        from core.services.backup import database_restore
        f = request.FILES.get("backup_file")
        confirm = request.POST.get("confirm", "").strip().upper()
        if confirm != "RESTORE":
            messages.error(request, 'Type RESTORE to confirm — the database was not changed.')
            return redirect("settings")
        if not f:
            messages.error(request, "Choose a backup file to restore.")
            return redirect("settings")
        ok, msg = database_restore(f)
        if ok:
            messages.success(request, msg)
        else:
            messages.error(request, msg)
        return redirect("settings")


class UpdateRunView(TreasurerRequiredMixin, View):
    """Start an in-app update (button-triggered) and show a live progress page."""
    template_name = "update_run.html"

    def get(self, request):
        from core.services.updates import update_status, update_available, latest_release
        from django.conf import settings
        # visiting the update page = an explicit "check now", so bypass the cache
        avail, tag, cur = update_available(force=True)
        # diagnostics so the treasurer can see WHY no update shows
        repo = getattr(settings, "GITHUB_REPO", "") or ""
        token_set = bool(getattr(settings, "GITHUB_TOKEN", "") or "")
        rel = latest_release(force=True)
        if not repo:
            diag = "No GITHUB_REPO is configured, so the app can't check for updates."
        elif rel is None:
            diag = (f"Couldn't read releases from '{repo}'. If the repository is "
                    f"private, set GITHUB_TOKEN in the server's .env. If it's public, "
                    f"make sure a Release (not just a tag) has been published on GitHub.")
        elif not tag:
            diag = (f"Connected to '{repo}', but no published Release was found. "
                    f"Publish a Release on GitHub (tags alone aren't enough).")
        else:
            diag = None
        return render(request, self.template_name, {
            "status": update_status(), "update_tag": tag,
            "current": cur, "available": avail,
            "repo": repo, "token_set": token_set, "diag": diag})

    def post(self, request):
        from core.services.updates import start_update
        from django.http import JsonResponse
        result = start_update()
        if result is None:
            messages.error(request, "This instance isn't a git checkout, so it "
                           "can't update itself. Update from the server with ./update.sh.")
            return redirect("settings")
        if result is False:
            messages.info(request, "An update is already running.")
        return redirect("update_run")


class UpdateStatusView(TreasurerRequiredMixin, View):
    """JSON polling endpoint for the update progress page."""
    def get(self, request):
        from core.services.updates import update_status
        from django.http import JsonResponse
        return JsonResponse(update_status())


class HealthCheckView(View):
    """Lightweight health/readiness probe for hosting platforms and uptime
    monitors. Returns 200 with a tiny JSON body if the app and database are up.
    No authentication required; exposes no sensitive data."""
    def get(self, request):
        from django.http import JsonResponse
        from django.db import connection
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        from core.version import get_version
        status = 200 if db_ok else 503
        return JsonResponse({"status": "ok" if db_ok else "degraded",
                             "database": db_ok, "version": get_version()},
                            status=status)


# ---- custom error handlers (witty pages + admin alert on 500) --------------
def error_500(request):
    """500 handler: alert the admin (best-effort) then render the friendly page."""
    from django.shortcuts import render
    try:
        from core.services.notifications import alert_admins_error
        alert_admins_error(f"server error on {request.path}")
    except Exception:
        pass
    return render(request, "500.html", status=500)


def error_404(request, exception=None):
    from django.shortcuts import render
    return render(request, "404.html", status=404)


def error_403(request, exception=None):
    from django.shortcuts import render
    return render(request, "403.html", status=403)
