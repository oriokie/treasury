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

        # Semantic Reporting Layer: obtain headline figures through a single
        # ReportContext so the dashboard shares the exact metrics (and their
        # request-scoped memoization) that the reports use — guaranteeing a
        # dashboard figure equals the corresponding report figure by
        # construction (recommendation #24). The underlying services are
        # unchanged; the registry metrics wrap them.
        from core.reporting import ReportContext
        rc = ReportContext.for_period(start, end)

        rows = rc.fund_summary(consolidated=True)
        ctx["start"], ctx["end"] = start, end
        ctx["totals"] = balances.totals(rows)
        ctx["trust_rows"] = rc.trust_summary()
        ctx["trust_to_remit"] = rc.trust_to_remit()
        ctx["local_rows"] = sorted(
            [r for r in rows if not r["is_trust"]],
            key=lambda r: r["closing"] or 0, reverse=True)
        ctx["local_totals"] = balances.totals(ctx["local_rows"])
        # item 6: hide the expenses column on the dashboard when every local fund
        # shown is a collection-only account (opening + receipts = closing)
        ctx["local_show_expenses"] = not all(
            getattr(r["department"], "collection_only", False)
            for r in ctx["local_rows"]) if ctx["local_rows"] else True
        # expenses still awaiting a supporting document (item 3)
        from cashbook.views import missing_receipts_queryset
        from django.db.models import Sum as _Sum
        _mr = missing_receipts_queryset(start, end)
        ctx["missing_receipts_count"] = _mr.count()
        ctx["missing_receipts_value"] = _mr.aggregate(s=_Sum("amount"))["s"] or 0
        from core.models import SiteConfig
        ctx["field_name"] = SiteConfig.get().field_name or "conference"
        ctx["by_group"] = rc.metric("giving_by_group", start, end)
        ctx["by_channel"] = rc.income_by_channel()
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
        ctx["tithe"] = rc.tithe()

        # per-user dashboard widget visibility + order
        try:
            from core.models import UserPreference
            pref = UserPreference.get_for(self.request.user)
            wlist = pref.merged_widgets() if pref else UserPreference.DEFAULT_WIDGETS
        except Exception:  # noqa: BLE001
            from core.utils import log_exception as _lx; _lx("dashboard widgets")
            wlist = []
        ctx["widget_visible"] = [w["key"] for w in wlist if w.get("visible", True)]
        ctx["widget_order"] = {w["key"]: i for i, w in enumerate(wlist)}

        # live cleared balance from the real-time bank feed (if any)
        try:
            from statements.services.importer import latest_cleared_balance
            ctx["live_balance"] = latest_cleared_balance()
        except Exception:  # noqa: BLE001
            from core.utils import log_exception as _lx; _lx("dashboard live balance")
            ctx["live_balance"] = None

        # --- extra dashboard insight data (item 6) ---
        from decimal import Decimal
        from django.db.models import Sum as _Sum
        # income by channel (doughnut)
        _chan = {c["channel"]: float(c["total"] or 0) for c in ctx["by_channel"]}
        ctx["channel_json"] = safe_json([
            {"label": "Bank", "value": _chan.get("BANK", 0)},
            {"label": "Envelope", "value": _chan.get("ENVELOPE", 0)},
            {"label": "Cash", "value": _chan.get("CASH", 0)},
        ])
        # local vs trust receipts for the selected period (doughnut) — moved
        # here in place of the receipts-vs-expenses-by-month chart, which now
        # lives on the Executive overview showing the full year instead of
        # just the currently-selected (usually one-month) period.
        _lt = (Transaction.objects.confirmed_credits()
               .filter(date__gte=start, date__lte=end, excluded_from_income=False)
               .values("department__fund_type").annotate(t=_Sum("amount")))
        _lt_map = {r["department__fund_type"]: float(r["t"] or 0) for r in _lt}
        ctx["local_trust_json"] = safe_json({
            "local": _lt_map.get("LOCAL", 0), "trust": _lt_map.get("TRUST", 0)})
        ctx["has_local_trust"] = bool(_lt_map.get("LOCAL") or _lt_map.get("TRUST"))
        # top funds by receipts (horizontal bars)
        _top = sorted(ctx["local_rows"], key=lambda r: r["receipts"], reverse=True)[:8]
        ctx["topfunds_json"] = safe_json({
            "labels": [r["department"].name[:22] for r in _top],
            "values": [float(r["receipts"]) for r in _top]})
        ctx["has_topfunds"] = bool(_top)
        # trust outstanding total for the KPI strip
        ctx["trust_outstanding"] = sum((r["to_remit"] for r in ctx["trust_rows"]), Decimal(0))

        ctx["queue_count"] = Transaction.objects.filter(
            allocation_status=Transaction.Status.REVIEW).exclude(
            direction=Transaction.Direction.DEBIT,
            channel=Transaction.Channel.BANK).count()
        ctx["debit_count"] = Transaction.objects.filter(
            direction=Transaction.Direction.DEBIT,
            channel=Transaction.Channel.BANK,
            allocation_status=Transaction.Status.REVIEW).count()
        ctx["pending_expenses"] = Expense.objects.filter(
            status=Expense.Status.PENDING).count()
        ctx["dup_count"] = PossibleDuplicate.objects.filter(resolved=False).count()
        ctx["member_count"] = Member.objects.filter(active=True).count()
        ctx["fund_count"] = Department.objects.filter(active=True).count()
        ctx["recent_imports"] = StatementImport.objects.all()[:5]

        # --- "This Sabbath" snapshot: the latest Sabbath's recognised collection,
        # compared to the one before, with a short fund breakdown. Uses the same
        # income basis as the rest of the app (recognised credits, excluding the
        # envelope-twin rows) so it can never double-count. All grouped queries.
        import datetime as dt
        today = dt.date.today()
        last_sab = today - dt.timedelta(days=(today.weekday() - 5) % 7)  # most recent Sat
        prev_sab = last_sab - dt.timedelta(days=7)
        sab_base = (Transaction.objects.confirmed_credits()
                    .filter(excluded_from_income=False))
        from django.db.models import Sum as _SumAgg
        sab_this = (sab_base.filter(service_sabbath=last_sab)
                    .aggregate(t=_SumAgg("amount"))["t"] or Decimal(0))
        sab_prev = (sab_base.filter(service_sabbath=prev_sab)
                    .aggregate(t=_SumAgg("amount"))["t"] or Decimal(0))
        sab_delta = sab_this - sab_prev
        sab_pct = (float(sab_delta) / float(sab_prev) * 100) if sab_prev else None
        sab_funds = list(
            sab_base.filter(service_sabbath=last_sab, department__isnull=False)
            .values("department__name").annotate(t=_SumAgg("amount")).order_by("-t")[:4])
        sab_gifts = sab_base.filter(service_sabbath=last_sab).count()
        try:
            from envelopes.models import Envelope
            sab_envelopes = Envelope.objects.filter(date=last_sab).count()
        except Exception:
            from core.utils import log_exception as _lx; _lx('core/views.py')
            sab_envelopes = 0
        ctx["sabbath"] = {
            "date": last_sab, "prev_date": prev_sab,
            "total": sab_this, "prev_total": sab_prev,
            "delta": sab_delta, "pct": sab_pct,
            "up": sab_delta >= 0, "funds": sab_funds,
            "gifts": sab_gifts, "envelopes": sab_envelopes,
            "has_data": bool(sab_this or sab_gifts),
        }
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
            from core.utils import log_exception as _lx; _lx('core/views.py')
            ctx["pledge_draft_count"] = 0
            ctx["pledge_overdue_count"] = 0

        # --- consolidated "needs attention" list ------------------------------
        # One place that surfaces everything quietly rotting, each with a count,
        # tone and link. Only non-zero items appear.
        from django.urls import reverse as _rev
        attention = []
        if ctx["queue_count"]:
            attention.append({"label": "giving items need allocating",
                "count": ctx["queue_count"], "tone": "warn",
                "url": _rev("queue"), "icon": "◷"})
        if ctx.get("debit_count"):
            attention.append({"label": "bank debits need classifying",
                "count": ctx["debit_count"], "tone": "warn",
                "url": _rev("debit_queue"), "icon": "◷"})
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

        # multi-year trend, compared like-for-like THROUGH THE CURRENT MONTH so a
        # year still in progress isn't measured against full prior years.
        import json, datetime as _dt, calendar as _cal
        from decimal import Decimal
        from django.db.models import Sum
        from core.models import HistoricalYear, HistoricalMonth
        MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Oct", "Nov", "Dec"]
        today = _dt.date.today()
        cur_year, cur_month = today.year, today.month

        def _txn_through_month(year):
            ys = _dt.date(year, 1, 1)
            ye = _dt.date(year, cur_month, _cal.monthrange(year, cur_month)[1])
            cc = (Transaction.objects.confirmed_credits().filter(
                  date__gte=ys, date__lte=ye, excluded_from_income=False)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
            ct = (Transaction.objects.confirmed_credits().filter(
                  date__gte=ys, date__lte=ye, department__fund_type="TRUST",
                  excluded_from_income=False).aggregate(t=Sum("amount"))["t"] or Decimal(0))
            ce = (Expense.objects.filter(status__in=[Expense.Status.APPROVED,
                  Expense.Status.PAID], date__gte=ys, date__lte=ye)
                  .exclude(doc_class=Expense.DocClass.LIABILITY)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
            return cc, ct, ce

        hist_years = {h.year: h for h in HistoricalYear.objects.all()}
        hm_years = set(HistoricalMonth.objects.values_list("year", flat=True).distinct())
        trend, approx = [], False
        for y in sorted(set(hist_years) | hm_years | {cur_year}):
            if y == cur_year:
                cc, ct, ce = _txn_through_month(y)
            elif y in hm_years:
                a = (HistoricalMonth.objects.filter(year=y, month__lte=cur_month)
                     .aggregate(c=Sum("collection"), t=Sum("trust_fund"),
                                e=Sum("expenditure")))
                cc, ct, ce = (a["c"] or Decimal(0), a["t"] or Decimal(0),
                              a["e"] or Decimal(0))
            else:
                # annual-only history: pro-rate to the elapsed share of the year
                h = hist_years[y]; frac = Decimal(cur_month) / Decimal(12)
                cc, ct, ce = (h.collection * frac, h.trust_fund * frac,
                              h.expenditure * frac)
                approx = True
            if cc or ct or ce:
                trend.append({"year": y, "collection": float(cc),
                              "trust": float(ct), "expenditure": float(ce)})
        trend.sort(key=lambda t: t["year"])
        ctx["trend_json"] = safe_json(trend)
        ctx["has_trend"] = len(trend) >= 2
        ctx["trend_through"] = MN[cur_month - 1]
        ctx["trend_approx"] = approx
        return ctx


# ---- System configuration ----
from django.contrib import messages
from django.http import JsonResponse
from django.urls import reverse
from django.shortcuts import redirect, render
from django.views import View

from core.utils import safe_json
from core.permissions import (TreasurerRequiredMixin, ReadAccessMixin,
                              DataEntryRequiredMixin, ExecutiveAccessMixin,
                              ElderRequiredMixin)
from core.models import SiteConfig, SmsLog
from core.forms import SiteConfigForm


def _unplaced_setting_fields(form):
    """Bound SiteConfigForm fields that the settings template does NOT render in
    any tab — so the template can show them in a fallback "Other settings" panel
    rather than let them vanish (recommendation #74a). Self-maintaining: it reads
    the template and treats a field as placed if the template references it, in a
    tab's ``f.name in '…'`` allowlist OR as an explicit ``form.<name>``. So a new
    setting shows up automatically — in its proper tab once added there, in the
    fallback panel until then — and is never silently unreachable.
    """
    import functools
    import pathlib
    import re

    @functools.lru_cache(maxsize=1)
    def _placed_names(mtime):
        tpl = pathlib.Path(__file__).resolve().parent.parent / "templates" / "settings.html"
        text = tpl.read_text()
        placed = set()
        for grp in re.findall(r"f\.name in '([^']+)'", text):
            placed |= set(grp.split())
        placed |= set(re.findall(r"form\.([a-z0-9_]+)", text))
        return placed

    tpl = pathlib.Path(__file__).resolve().parent.parent / "templates" / "settings.html"
    placed = _placed_names(tpl.stat().st_mtime)
    return [form[name] for name in form.fields if name not in placed]


class SettingsView(TreasurerRequiredMixin, View):
    template_name = "settings.html"

    def get(self, request):
        cfg = SiteConfig.get()
        from core.models import TelegramProfile
        from django.contrib.auth.models import User
        from core.roles import role_label
        mine = TelegramProfile.objects.filter(user=request.user).first()
        tg_users = []
        # Two queries for the whole list, not two per user: `telegram_profile`
        # is a reverse one-to-one that was being fetched per row, and
        # `role_label` reads the user's groups. This page grows with the staff
        # list, so on a church with real user accounts it was the fund register
        # problem again in a different place.
        staff = (User.objects.filter(is_active=True)
                 .select_related("telegram_profile")
                 .prefetch_related("groups")
                 .order_by("username"))
        for u in staff:
            prof = getattr(u, "telegram_profile", None)
            tg_users.append({"name": u.get_full_name() or u.username,
                             "role": role_label(u),
                             "has_pin": bool(prof and prof.pin)})
        camp_progress = None
        if cfg.camp_offering_goal and cfg.camp_offering_fund_id:
            from reports.views import _camp_goal_records
            import datetime as _dt
            rows = _camp_goal_records(_dt.date.today().year)
            camp_progress = next((r for r in rows if r["kind"] == "Offering (trust)"), None)
        # Bound once. The form carries several fund selectors, so building it
        # twice ran every one of those querysets twice for the same result.
        form = SiteConfigForm(instance=cfg)
        return render(request, self.template_name, {
            "form": form,
            "unplaced_settings": _unplaced_setting_fields(form),
            "recent_sms": SmsLog.objects.all()[:10],
            "cbs_webhook_url": request.build_absolute_uri(reverse("cbs_webhook")),
            "my_telegram_pin": mine.pin if mine else "",
            "telegram_users": tg_users,
            "camp_offering_progress": camp_progress,
        })

    def post(self, request):
        cfg = SiteConfig.get()
        if "send_test" in request.POST:
            from core.services.sms import send_sms
            to = request.POST.get("test_to", "").strip()
            log = send_sms(to, "Test message from the church treasury system.", cfg)
            messages.info(request, f"Test SMS: {log.get_status_display()} — {log.response[:120]}")
            return redirect(reverse("settings") + "?tab=sms")
        if "test_email" in request.POST:
            from core.services.email import test_email
            form = SiteConfigForm(request.POST, instance=cfg)
            if form.is_valid():
                cfg = form.save()
            to = (request.POST.get("email_test_to") or cfg.email_from or "").strip()
            ok, detail = test_email(to, cfg)
            (messages.success if ok else messages.error)(
                request, f"Email test {'sent' if ok else 'failed'} — {detail}")
            return redirect(reverse("settings") + "?tab=email")
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
            return redirect(reverse("settings") + "?tab=assistant")
        form = SiteConfigForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            messages.success(request, "Settings saved.")
            tab = (request.POST.get("active_tab") or "").strip()
            url = reverse("settings")
            if tab:
                url += f"?tab={tab}"
            return redirect(url)
        return render(request, self.template_name, {
            "form": form, "recent_sms": SmsLog.objects.all()[:10]})


class MemberSearchView(DataEntryRequiredMixin, View):
    """JSON typeahead for contributor name fields (up to 5 suggestions).

    Searches ALTERNATE phone numbers (`MemberPhone`) as well as the primary
    one. A member who pays from a second line is still that member — the
    `MemberPhone` table exists precisely to record that, and
    `match_or_create_member()` has always matched a bank narration against it.
    But this search only ever looked at `Member.phone`, so a treasurer typing
    the very number that appears in the narration in front of them would find
    nobody, and would be pushed into creating a duplicate for a person the
    system already knew.
    """

    def get(self, request):
        from django.db.models import Q
        from members.models import Member
        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})
        qs = (Member.objects.filter(active=True)
              .filter(Q(name__icontains=q) | Q(phone__icontains=q)
                     | Q(phones__number__icontains=q))
              .distinct()
              .prefetch_related("phones")
              .order_by("name")[:5])
        from core.rights import display_phone
        results = [{"id": m.id, "name": m.name,
                    "phone": display_phone(request.user, m.receipt_phone or m.phone or ""),
                    "type": m.get_member_type_display() if m.member_type else ""}
                   for m in qs]
        return JsonResponse({"results": results})


class NextReceiptView(DataEntryRequiredMixin, View):
    """Return the next sequential envelope receipt number — used only to
    PRE-FILL the entry grid's first row (which the cashier can freely
    overtype); it also steers clear of numbers another open batch has already
    claimed, so two treasurers working concurrently are less likely to land
    on the same suggestion (the authoritative duplicate check still runs at
    Submit/Approve/Post regardless)."""

    def get(self, request):
        from envelopes.models import Envelope, EnvelopeBatch, EnvelopeBatchRow
        nums = []
        for r in Envelope.objects.values_list("receipt_no", flat=True):
            digits = "".join(ch for ch in str(r) if ch.isdigit())
            if digits:
                nums.append(int(digits))
        for r in (EnvelopeBatchRow.objects
                  .exclude(batch__status__in=[EnvelopeBatch.Status.POSTED,
                                              EnvelopeBatch.Status.REJECTED])
                  .values_list("receipt_no", flat=True)):
            digits = "".join(ch for ch in str(r) if ch.isdigit())
            if digits:
                nums.append(int(digits))
        nxt = (max(nums) + 1) if nums else 1
        return JsonResponse({"next": str(nxt)})


class TableStateView(LoginRequiredMixin, View):
    """Get/set one keyed slice of the current user's
    ``UserPreference.table_state`` — a small, generic per-user layout store
    (column order/visibility/width/pinned columns, sort, filters) any
    data-grid template can use; a preference is tied to the *account*, so it
    follows the person across devices and is restored automatically on every
    future login, not just cached in one browser. First real consumer: the
    envelope ledger entry grid's customisable columns.

    ``table_key`` is a short slug the calling template picks (e.g.
    ``"envelope_ledger_grid"``); the stored value is whatever JSON object the
    caller sends — this view doesn't interpret its shape, only stores it."""

    def get(self, request, table_key):
        from core.models import UserPreference
        pref = UserPreference.get_for(request.user)
        state = (pref.table_state or {}).get(table_key) or {}
        return JsonResponse({"ok": True, "state": state})

    def post(self, request, table_key):
        import json
        from core.models import UserPreference
        try:
            state = json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)
        if not isinstance(state, dict):
            return JsonResponse({"ok": False, "error": "State must be an object."},
                                status=400)
        pref = UserPreference.get_for(request.user)
        ts = dict(pref.table_state or {})
        ts[table_key] = state
        pref.table_state = ts
        pref.save(update_fields=["table_state"])
        return JsonResponse({"ok": True})


class DepartmentBalanceView(DataEntryRequiredMixin, View):
    """JSON: a department's available balance for the expense form to show on
    selection. Reads the CANONICAL single-fund calculation
    (reports.services.balances.fund_balance_parts — the fund_balance registry
    metric's basis), so this figure always equals the fund's balance on the
    departments page and every report. The previous inline calculation here had
    drifted: it still counted reversed/reversal credits as receipts, excluded
    remittance expenses from spend, and ignored refunds — overstating the
    available balance whenever any of those existed."""

    def get(self, request):
        from departments.models import Department
        from reports.services.balances import fund_balance_parts
        try:
            dept = Department.objects.get(pk=request.GET.get("id"))
        except (Department.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"ok": False})
        p = fund_balance_parts(dept)
        return JsonResponse({
            "ok": True, "name": dept.name,
            "fund_type": "Trust" if dept.is_trust else "Local",
            "opening": float(p["opening"] or 0),
            "receipts": float(p["receipts"]), "spent": float(p["spent"]),
            "refunded": float(p["refunded"]),
            "transfers_in": float(p["transfers_in"]),
            "transfers_out": float(p["transfers_out"]),
            "balance": float(p["balance"])})


class AssistantView(ReadAccessMixin, View):
    """Chat-style assistant page. Optional URL params (report_key, start, end,
    element, q) let a report's 'Ask AI' button open the assistant already aware
    of what the user was looking at."""
    def get(self, request):
        from .services.assistant import SUGGESTIONS, suggestion_groups_for_site
        from core.models import SiteConfig
        report_key = request.GET.get("report_key", "")
        ctx = {
            "suggestions": SUGGESTIONS,
            "suggestion_groups": suggestion_groups_for_site(),
            "llm_on": SiteConfig.get().llm_enabled,
            "ctx_report_key": report_key,
            "ctx_start": request.GET.get("start", ""),
            "ctx_end": request.GET.get("end", ""),
            "ctx_element": request.GET.get("element", ""),
            "ctx_prefill": request.GET.get("q", ""),
        }
        return render(request, "assistant.html", ctx)


class AssistantAskView(ReadAccessMixin, View):
    """Answer one question as JSON. Accepts optional report context (report_key,
    period, element) so the assistant answers questions about the report the user
    is currently viewing without them restating what they mean."""
    def post(self, request):
        import json as _json
        from .services import assistant
        try:
            payload = _json.loads(request.body.decode() or "{}")
        except Exception:
            from core.utils import log_exception as _lx; _lx('core/views.py')
            payload = {"q": request.POST.get("q", "")}
        q = payload.get("q", "")
        report_key = payload.get("report_key")
        period = payload.get("period")
        element = payload.get("element")
        try:
            if report_key or element:
                # report-context-aware answering via the Knowledge Service
                from .services.assistant_knowledge import answer_with_context
                data = answer_with_context(q, report_key=report_key,
                                           period=period, element=element,
                                           user=request.user)
            else:
                data = assistant.answer(q, request.user)
        except Exception as exc:
            from core.utils import log_exception as _lx; _lx('core/views.py')
            data = {"text": f"Sorry — I hit an error answering that ({type(exc).__name__})."}
        return JsonResponse(data)


class FundSearchView(ReadAccessMixin, View):
    """Typeahead for fund pickers. scope=income adds split funds as single
    options; scope=expense restricts to expense-eligible funds."""
    def get(self, request):
        from departments.models import expense_departments, income_departments, Department
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
                results.append({"key": f"d:{d.id}", "id": d.id, "label": d.name,
                                "tag": tag,
                                "dev": d.category == Department.Category.DEVELOPMENT})
        if scope == "income":
            from giving.models import SplitFund
            for s in SplitFund.objects.filter(active=True):
                if not q or q in s.name.lower():
                    results.append({"key": f"s:{s.id}", "id": s.id,
                                    "label": f"{s.name} (split)", "tag": "Split"})
        results.sort(key=lambda r: r["label"].lower())
        return JsonResponse({"results": results[:20]})


class GlobalSearchView(ReadAccessMixin, View):
    """Server-backed search across records (members, funds, staff advances,
    expenses, recent receipts) for the command palette — so a search finds the
    actual thing, not just a page. Capped per category for speed."""
    def get(self, request):
        from django.db.models import Q
        from django.urls import reverse
        q = (request.GET.get("q") or "").strip()
        out = []
        if len(q) < 2:
            return JsonResponse({"results": out})
        ql = q.lower()

        # Members ---------------------------------------------------------
        try:
            from members.models import Member
            mqs = (Member.objects.filter(
                Q(name__icontains=q) | Q(phone__icontains=q) |
                Q(envelope_no__icontains=q))
                .order_by("name")[:6])
            for m in mqs:
                sub = " · ".join(filter(None, [
                    m.phone or "", f"Env {m.envelope_no}" if getattr(m, "envelope_no", "") else ""]))
                out.append({"label": m.name, "sublabel": sub or "Member",
                            "group": "Members", "icon": "👤",
                            "href": reverse("report_member", args=[m.id])})
        except Exception:  # noqa: BLE001
            pass

        # Funds / departments --------------------------------------------
        try:
            from departments.models import Department
            for d in Department.objects.filter(name__icontains=q).order_by("name")[:6]:
                out.append({"label": d.name,
                            "sublabel": ("Trust fund" if d.is_trust else "Local fund")
                            + (" · closed" if d.status != "ACTIVE" else ""),
                            "group": "Funds", "icon": "🏦",
                            "href": reverse("report_fund", args=[d.id])})
        except Exception:  # noqa: BLE001
            pass

        # Staff advances --------------------------------------------------
        try:
            from cashbook.models import StaffAdvance
            for a in (StaffAdvance.objects.filter(
                    Q(staff_name__icontains=q) | Q(purpose__icontains=q) |
                    Q(reference__icontains=q)).order_by("-date_issued")[:6]):
                out.append({"label": f"Advance · {a.staff_name}",
                            "sublabel": f"{a.department.name} · {a.get_status_display()} · "
                                        f"bal {a.balance:,.0f}",
                            "group": "Staff advances", "icon": "💵",
                            "href": reverse("advance_detail", args=[a.id])})
        except Exception:  # noqa: BLE001
            pass

        # Expenses --------------------------------------------------------
        try:
            from cashbook.models import Expense
            for x in (Expense.objects.filter(
                    Q(description__icontains=q) | Q(claimant__icontains=q) |
                    Q(voucher_no__icontains=q)).select_related("department")
                    .order_by("-date")[:6]):
                out.append({"label": x.description,
                            "sublabel": f"{x.department.name} · {x.date:%d %b %Y} · "
                                        f"KSh {x.amount:,.0f}",
                            "group": "Expenses", "icon": "🧾",
                            "href": reverse("expense_detail", args=[x.id])})
        except Exception:  # noqa: BLE001
            pass

        # Recent receipts by reference / payer ----------------------------
        try:
            from giving.models import Transaction
            for t in (Transaction.objects.filter(
                    Q(reference__icontains=q) | Q(payer_name__icontains=q) |
                    Q(mpesa_ref__icontains=q) | Q(core_ref__icontains=q)).select_related("department")
                    .order_by("-date")[:6]):
                who = t.payer_name or t.reference or "Receipt"
                out.append({"label": who,
                            "sublabel": f"{t.department.name if t.department else 'Unallocated'} · "
                                        f"{t.date:%d %b %Y} · KSh {t.amount:,.0f}",
                            "group": "Receipts", "icon": "↘",
                            "href": reverse("transaction_list") + f"?q={who}"})
        except Exception:  # noqa: BLE001
            pass

        return JsonResponse({"results": out[:30]})


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
        from core.services.period_close import period_close_checklist, checklist_all_clear
        year = int(request.GET.get("year") or dt.date.today().year)
        locks = {l.month: l for l in PeriodLock.objects.filter(year=year)}
        months = [{"num": m, "name": calendar.month_name[m], "lock": locks.get(m)}
                  for m in range(1, 13)]
        # period-close checklist for whichever month is about to be locked/viewed
        checklist_month = int(request.GET.get("checklist_month") or dt.date.today().month)
        checklist = period_close_checklist(year, checklist_month) if not locks.get(checklist_month) else None
        # year-end close: preview the balances that would carry forward
        close_year = int(request.GET.get("close_year") or (dt.date.today().year - 1))
        cf_rows = balances.department_summary(dt.date(close_year, 1, 1),
                                              dt.date(close_year, 12, 31))
        cf_total = sum((r["closing"] for r in cf_rows), Decimal(0))
        return render(request, self.template_name, {
            "year": year, "months": months,
            "years": range(dt.date.today().year + 1, dt.date.today().year - 5, -1),
            "close_year": close_year,
            "cf_rows": cf_rows, "cf_total": cf_total,
            "closes": YearEndClose.objects.all(),
            "is_closed": (lambda c: bool(c and c.is_effective))(
                YearEndClose.objects.filter(year=close_year).first()),
            "pending_close": (lambda c: c if (c and not c.is_effective) else None)(
                YearEndClose.objects.filter(year=close_year).first()),
            "checklist_month": checklist_month,
            "checklist_month_name": calendar.month_name[checklist_month],
            "checklist": checklist,
            "checklist_all_clear": checklist_all_clear(checklist) if checklist else True,
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
               .exclude(doc_class=Expense.DocClass.LIABILITY)
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
    from core.utils import sabbath_of
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
        sab = sabbath_of(r["date"])
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


class ControlsDuplicatesView(TreasurerRequiredMixin, View):
    """Computes possible duplicates on demand (button-triggered) so the controls
    page itself loads fast and these heavier scans run only when asked for."""
    def get(self, request, kind):
        if kind == "expenses":
            return render(request, "controls/_dup_expenses.html",
                          {"dup_expenses": _duplicate_expenses()})
        if kind == "offerings":
            return render(request, "controls/_dup_offerings.html",
                          {"dup_offerings": _duplicate_offerings()})
        from django.http import HttpResponseBadRequest
        return HttpResponseBadRequest("unknown check")


def _duplicate_offerings(window_days=7):
    """Likely duplicate offerings - only signals that actually indicate a double
    count, so neither a shared paybill reference nor two genuinely separate gifts
    of the same amount weeks apart are flagged.

    A true bank+envelope double count is the SAME gift counted on BOTH channels:
    same giver, same amount, and close in time. We therefore require the bank and
    envelope entries to fall within `window_days` of each other (not merely the
    same calendar month, which produced many false positives - #14).

    A split offering (e.g. Combined Offering) is one bank credit posted across
    several funds as sibling rows. Those siblings are collapsed back into a single
    gift here (summing the parts) so the halves of a split are never mistaken for
    duplicates of each other or of the envelope that receipts the whole split
    (#13). A bank credit properly reconciled to its envelope is a memo
    (excluded_from_income) and never reaches here at all.
    """
    from collections import defaultdict
    from giving.models import Transaction
    from members.services.matching import name_key

    rows = list(Transaction.objects.filter(
                    direction=Transaction.Direction.CREDIT, is_reversal=False,
                    is_reversed=False, excluded_from_income=False, confirmed=True)
                .values("id", "date", "amount", "payer_name", "member__name",
                        "channel", "reference", "core_ref", "mpesa_ref"))

    def _split_key(r):
        if r["core_ref"]:
            return ("cref", r["core_ref"].split("-S")[0])
        if r["mpesa_ref"]:
            return ("mref", r["mpesa_ref"].lower(), r["date"])
        if r["reference"]:
            return ("ref", r["reference"].lower(), r["date"], r["channel"])
        return ("id", r["id"])

    collapsed = {}
    for r in rows:
        k = _split_key(r)
        if k in collapsed:
            collapsed[k]["amount"] = (collapsed[k]["amount"] or 0) + (r["amount"] or 0)
        else:
            collapsed[k] = dict(r)
    gifts = list(collapsed.values())

    by_name = defaultdict(list)
    for g in gifts:
        name = g["payer_name"] or g["member__name"] or ""
        nk = name_key(name)
        if not nk or not g["amount"]:
            continue
        by_name[nk].append(g)

    out = []
    for nk, grp in by_name.items():
        banks = [g for g in grp if g["channel"] == Transaction.Channel.BANK]
        envs = [g for g in grp if g["channel"] == Transaction.Channel.ENVELOPE]
        used_env = set()
        for b in banks:
            for ev in envs:
                if id(ev) in used_env:
                    continue
                if b["amount"] == ev["amount"] and \
                        abs((b["date"] - ev["date"]).days) <= window_days:
                    used_env.add(id(ev))
                    first = min(b["date"], ev["date"])
                    last = max(b["date"], ev["date"])
                    out.append({
                        "date": last, "first": first, "amount": b["amount"],
                        "payer": (b["payer_name"] or b["member__name"]
                                  or ev["payer_name"] or ev["member__name"] or "-"),
                        "count": 2, "by": f"bank + envelope within {window_days} days",
                        "reference": b["reference"] or ev["reference"] or "-",
                        "channel": "bank+envelope"})
                    break

    out.extend(_duplicate_envelopes())
    out.sort(key=lambda c: ((c.get("payer") or "~").upper(), c["date"]))
    return out[:100]


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


class ElderDashboardView(ElderRequiredMixin, TemplateView):
    """A simple, board-level landing page for church elders: a handful of the
    same headline KPIs shown on the Executive overview, plus a prominent link
    there for full detail. Elders get this and the Executive overview by
    default; broader "reports" access is a separately assignable right a
    treasurer can grant to a specific elder, not switched on for everyone."""
    template_name = "elder_dashboard.html"

    def get_context_data(self, **kwargs):
        from .services import dashboard
        from .rights import has_right
        ctx = super().get_context_data(**kwargs)
        all_cards = dashboard.cards()
        # a curated subset — the figures a board member actually wants at a
        # glance, not the full operational KPI strip built for a treasurer
        wanted = {"Collections this month", "Collections (year to date)",
                  "Cash & bank balance", "Outstanding trust"}
        ctx["cards"] = [c for c in all_cards if c["label"] in wanted]
        ctx["can_view_reports"] = has_right(self.request.user, "view_reports")
        return ctx


class ExecutiveDashboardView(ExecutiveAccessMixin, View):
    """Executive overview: KPI cards, Chart.js trends, and health/anomaly alerts."""
    template_name = "executive.html"

    @staticmethod
    def _key(a):
        from django.utils.text import slugify
        return a.get("key") or slugify(a.get("title", ""))[:60]

    def get(self, request):
        import json
        from decimal import Decimal
        from .services import dashboard, health, forecast
        from core.models import SiteConfig
        from pledges.models import Pledge
        ch = dashboard.charts()
        pledges_out = sum((p.outstanding for p in
                           Pledge.objects.filter(status=Pledge.Status.ACTIVE)), Decimal(0))
        from cashbook.views import outstanding_advances_total, _petty_balance_asof
        import datetime as _dt
        _today = _dt.date.today()
        return render(request, self.template_name, {
            "forecast": forecast.horizons(),
            "pledges_outstanding": pledges_out,
            "advances_outstanding": outstanding_advances_total(_today),
            "petty_balance": _petty_balance_asof(_today),
            "cards": dashboard.cards(),
            "insights": dashboard.insights(),
            "kpis": health.kpis(),
            "facts": dashboard.quick_facts(),
            "charts_json": safe_json(ch),
            "ai_enabled": SiteConfig.get().llm_enabled,
        })

    def post(self, request):
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
        # only unread are shown — marking read makes them disappear from here
        qs = Notification.objects.filter(
            Q(recipient=request.user) | Q(recipient__isnull=True),
            read=False).order_by("-created_at")[:100]
        return render(request, self.template_name, {"notifications": qs})

    def post(self, request):
        from django.db.models import Q
        from core.models import Notification
        base = Notification.objects.filter(
            Q(recipient=request.user) | Q(recipient__isnull=True), read=False)
        one = request.POST.get("id")
        if one:
            base.filter(pk=one).update(read=True)
        else:
            base.update(read=True)
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
        if not ok:
            messages.error(request, msg)
            return redirect("settings")

        # A successful restore has just replaced the database — and both the
        # session and the flash messages live in it.
        #
        # This request's own session row was written when the treasurer signed
        # in, which was necessarily AFTER the backup was taken, so it does not
        # exist in the file that has just been put in its place. Django's
        # session middleware then tries to save that session at the end of the
        # request, finds no row to update, and raises SessionInterrupted — which
        # Django renders as a 400. The restore had already succeeded, so the
        # treasurer saw a stack trace on a screen and their data half-arrived,
        # with nothing to say which.
        #
        # Signing out is not a workaround, it is the truth: the account that was
        # signed in a moment ago may not exist in the restored database, and if
        # it does its password is whatever it was when the backup was taken.
        # Flushing leaves an empty session, which the middleware clears rather
        # than saves, so there is nothing left to fail.
        from django.contrib.auth import logout as auth_logout
        auth_logout(request)

        # Rendered directly rather than redirected with a flash message: those
        # are stored in the session too, so the message would be written into
        # the very thing just emptied and never seen.
        safety = ""
        marker = "was saved as "
        if marker in msg:
            safety = msg.split(marker, 1)[1].rstrip(".")
        return render(request, "restore_done.html",
                      {"message": msg, "safety": safety})


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
            # What GitHub actually said, rather than a guess at it. The old
            # message advised setting GITHUB_TOKEN even when one was set and
            # being rejected, which is the least useful moment to be told that.
            from core.services.updates import last_failure_reason
            diag = last_failure_reason() or (
                f"Couldn't read releases or tags from '{repo}', and GitHub gave "
                f"no reason. Check the repository name (owner/name).")
        elif not tag:
            diag = f"Connected to '{repo}', but no releases or tags were found yet."
        else:
            diag = None
        return render(request, self.template_name, {
            "status": update_status(), "update_tag": tag,
            "current": cur, "available": avail,
            "repo": repo, "token_set": token_set, "diag": diag})

    def post(self, request):
        from core.services.updates import start_update
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


from django.contrib.auth.decorators import login_not_required as _login_not_required
from django.utils.decorators import method_decorator as _method_decorator


@_method_decorator(_login_not_required, name="dispatch")
class HealthCheckView(View):
    """Lightweight health/readiness probe for hosting platforms and uptime
    monitors. Returns 200 with a tiny JSON body if the app and database are up.
    No authentication required (exempt from the P1-1 login gate); exposes no
    sensitive data."""
    def get(self, request):
        from django.http import JsonResponse
        from django.db import connection
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            db_ok = True
        except Exception:
            from core.utils import log_exception as _lx; _lx('core/views.py')
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
        from core.utils import log_exception as _lx; _lx('core/views.py')
        pass
    return render(request, "500.html", status=500)


def error_404(request, exception=None):
    from django.shortcuts import render
    return render(request, "404.html", status=404)


def error_403(request, exception=None):
    from django.shortcuts import render
    return render(request, "403.html", status=403)


class OffsiteBackupNowView(TreasurerRequiredMixin, View):
    """Generate a backup now and upload it to the configured off-site storage."""
    def post(self, request):
        from core.services.backup import database_backup_bytes, upload_offsite
        from core.fields import encrypt
        import base64
        try:
            filename, data = database_backup_bytes()
        except RuntimeError as e:
            messages.error(request, f"Backup failed: {e}")
            return redirect("settings")
        token = encrypt(base64.b64encode(data).decode("ascii"))
        ok, detail = upload_offsite(filename + ".enc", token.encode("ascii"))
        (messages.success if ok else messages.error)(request, detail)
        return redirect("settings")


# ---------------------------------------------------------------------------
# Appearance & Preferences (per-user)
# ---------------------------------------------------------------------------
class PreferencesView(LoginRequiredMixin, View):
    """The Appearance & Preferences page. GET renders the form; POST saves the
    full form (non-JS fallback). Live changes use PreferenceUpdateView."""
    template_name = "preferences.html"

    def get(self, request):
        from core.models import UserPreference
        from core.forms import UserPreferenceForm
        pref = UserPreference.get_for(request.user)
        return render(request, self.template_name, {
            "form": UserPreferenceForm(instance=pref),
            "pref": pref,
            "widgets": pref.merged_widgets(),
            "accent_presets": UserPreference.ACCENT_PRESETS,
        })

    def post(self, request):
        from core.models import UserPreference
        from core.forms import UserPreferenceForm
        pref = UserPreference.get_for(request.user)
        if "reset" in request.POST:
            pref.reset_to_defaults()
            messages.success(request, "Preferences reset to defaults.")
            return redirect("preferences")
        form = UserPreferenceForm(request.POST, instance=pref)
        if form.is_valid():
            form.save()
            messages.success(request, "Preferences saved.")
            return redirect("preferences")
        return render(request, self.template_name, {
            "form": form, "pref": pref, "widgets": pref.merged_widgets(),
            "accent_presets": UserPreference.ACCENT_PRESETS})


class PreferenceUpdateView(LoginRequiredMixin, View):
    """Lightweight JSON endpoint to persist a single preference (or the widget
    order) live, without a page reload."""
    ALLOWED = {
        "theme", "accent", "accent_custom", "sidebar", "sidebar_style", "font_size", "font_family",
        "layout_width", "card_style", "landing_page", "rows_per_page",
        "density", "high_contrast", "reduced_motion", "large_targets",
        "focus_indicators", "toasts_enabled", "toast_duration",
        "desktop_notifications",
        "heading_font", "figure_font", "negatives", "table_stripes", "table_grid",
        "sticky_headers",
    }
    BOOLS = {"high_contrast", "reduced_motion", "large_targets",
             "focus_indicators", "toasts_enabled", "desktop_notifications",
             "table_stripes", "sticky_headers"}
    INTS = {"rows_per_page", "toast_duration"}

    def post(self, request):
        from django.http import JsonResponse
        from core.models import UserPreference
        pref = UserPreference.get_for(request.user)
        key = request.POST.get("key", "")
        # widget order / visibility
        if key == "dashboard_widgets":
            import json
            try:
                data = json.loads(request.POST.get("value", "[]"))
                cleaned = [{"key": str(w["key"]), "visible": bool(w.get("visible", True))}
                           for w in data if isinstance(w, dict) and w.get("key")]
                pref.dashboard_widgets = cleaned
                pref.save(update_fields=["dashboard_widgets", "updated_at"])
                return JsonResponse({"ok": True})
            except Exception:  # noqa: BLE001
                from core.utils import log_exception as _lx; _lx("pref widget save")
                return JsonResponse({"ok": False, "error": "bad data"}, status=400)
        if key not in self.ALLOWED:
            return JsonResponse({"ok": False, "error": "unknown key"}, status=400)
        raw = request.POST.get("value", "")
        if key in self.BOOLS:
            val = raw in ("1", "true", "True", "on", "yes")
        elif key in self.INTS:
            try:
                val = int(raw)
            except (TypeError, ValueError):
                return JsonResponse({"ok": False, "error": "not a number"}, status=400)
            if key == "rows_per_page":
                val = max(5, min(200, val))
            if key == "toast_duration":
                val = max(2, min(30, val))
        else:
            val = raw[:32]
        setattr(pref, key, val)
        pref.save(update_fields=[key, "updated_at"])
        return JsonResponse({"ok": True, "value": val})


class PostLoginRedirectView(LoginRequiredMixin, View):
    """Sends the user to their chosen landing page after login."""
    def get(self, request):
        from core.models import UserPreference
        from django.urls import reverse, NoReverseMatch
        from core import roles
        # A self-service member has exactly one landing page, and the landing
        # preference does not apply to them: every choice it offers is an office
        # screen the confinement middleware would refuse. Routed explicitly
        # here rather than left to that middleware to bounce — relying on the
        # bounce works, but it sends the member through a page they may not
        # open to get to the one they may, which is an odd first impression and
        # an accident waiting to be optimised away.
        if roles.is_portal_only(request.user):
            return redirect("portal_home")

        pref = UserPreference.get_for(request.user)
        target = (pref.landing_page if pref else "dashboard") or "dashboard"
        # leaders always land on their scoped dashboard
        if roles.is_leader(request.user) and not request.user.is_superuser:
            target = "leader_dashboard"
        valid = {c[0] for c in UserPreference.LANDING_CHOICES} | {"leader_dashboard"}
        if target not in valid:
            target = "dashboard"
        try:
            return redirect(reverse(target))
        except NoReverseMatch:
            return redirect("dashboard")


class CleanReceiptMessagesView(TreasurerRequiredMixin, View):
    """Re-run the configured strip-strings over every already-saved receipt
    message (Settings → branding). Lets the treasurer add a new boilerplate
    phrase and clean up messages imported before it was configured."""

    def post(self, request):
        from cashbook.models import ExpenseAttachment, clean_receipt_text
        changed = 0
        qs = ExpenseAttachment.objects.exclude(text="")
        for att in qs.iterator():
            cleaned = clean_receipt_text(att.text)
            if cleaned != att.text:
                # go through the queryset update to avoid re-triggering save()
                ExpenseAttachment.objects.filter(pk=att.pk).update(text=cleaned)
                changed += 1
        messages.success(request, f"Cleaned {changed} saved receipt message(s) "
                         f"out of {qs.count()} checked.")
        return redirect("/settings/?tab=branding")


class AllocationPriorityView(TreasurerRequiredMixin, View):
    """The order allocation sources are tried in, and a tester for it.

    Treasurer-level: this decides which fund money lands in, which is a
    statement-of-accounts question rather than a data-entry one.

    The tester is on the same page as the ordering on purpose. Reordering
    allocation blind is how a church fixes one wrong fund and creates two more;
    being able to type the reference that went astray, see which sources
    claimed it, reorder, and check again before saving is the whole workflow.
    """
    template_name = "core/allocation_priority.html"

    def _context(self, request, order=None, probe=None, problems=()):
        from core.models import SiteConfig
        from core.services import allocation_priority as ap

        cfg = SiteConfig.get()
        order = order or ap.parse_order(cfg.allocation_priority)
        return {
            "stages": [ap.STAGE_BY_KEY[k] for k in order],
            "order": order,
            "is_default": ap.is_default(cfg.allocation_priority),
            "problems": list(problems),
            "probe": probe,
            "probe_reference": request.POST.get("reference", "") or
                               request.GET.get("reference", ""),
            "probe_name": request.POST.get("name", "") or request.GET.get("name", ""),
            "probe_phone": request.POST.get("phone", "") or request.GET.get("phone", ""),
        }

    def get(self, request):
        from core.services import allocation_priority as ap
        probe = None
        ref = request.GET.get("reference", "").strip()
        if ref:
            probe = ap.explain(ref, name=request.GET.get("name", ""),
                               phone=request.GET.get("phone", ""))
        return render(request, self.template_name, self._context(request, probe=probe))

    def post(self, request):
        from core.models import SiteConfig
        from core.services import allocation_priority as ap

        action = request.POST.get("action") or "save"

        if action == "test":
            ref = (request.POST.get("reference") or "").strip()
            if not ref:
                messages.error(request, "Type a reference to test first.")
                return render(request, self.template_name, self._context(request))
            probe = ap.explain(ref, name=request.POST.get("name", ""),
                               phone=request.POST.get("phone", ""))
            return render(request, self.template_name,
                          self._context(request, probe=probe))

        if action == "reset":
            cfg = SiteConfig.get()
            cfg.allocation_priority = ""
            cfg.save(update_fields=["allocation_priority"])
            messages.success(request, "Allocation order restored to the built-in one.")
            return redirect("allocation_priority")

        keys = [k for k in request.POST.getlist("order") if k]
        problems = ap.validate(keys)
        if problems:
            # Re-rendered with what they tried, not with what is saved: being
            # bounced back to the stored order would lose the work and hide
            # which move was refused.
            return render(request, self.template_name,
                          self._context(request, order=ap.parse_order("\n".join(keys)),
                                        problems=problems))
        cfg = SiteConfig.get()
        cfg.allocation_priority = "" if keys == ap.default_order() else "\n".join(keys)
        cfg.save(update_fields=["allocation_priority"])
        messages.success(
            request, "Allocation order saved. It applies to money imported from "
                     "now on — contributions already allocated are unchanged.")
        return redirect("allocation_priority")


class EnvelopeColumnsView(TreasurerRequiredMixin, View):
    """Which fund columns a new Sabbath envelope sheet opens with.

    The list was a constant in the source (`envelopes.services.posting.PREFERRED`),
    so a church collecting under different headings re-picked its columns by
    hand on every new sheet — every Sabbath, for as long as it had been using
    the system.

    Treasurer-level, and deliberately only about what a sheet OPENS with: every
    column stays available on the sheet itself, so this can never stop money
    being recorded against a fund. It only decides what is already there.
    """
    template_name = "core/envelope_columns.html"

    def _context(self, chosen=None):
        from envelopes.services.posting import (PREFERRED, column_catalog,
                                                configured_default_keys)
        cols = column_catalog()
        chosen = chosen if chosen is not None else configured_default_keys()
        by_key = {c["key"]: c for c in cols}
        # Selected first, in the church's own order; then the rest to add from.
        selected = [by_key[k] for k in chosen if k in by_key]
        if not selected:
            selected = [c for c in cols if c["default"]]
        rest = [c for c in cols if c not in selected]
        return {
            "selected": selected,
            "available": sorted(rest, key=lambda c: c["label"].lower()),
            "is_default": not configured_default_keys(),
            "built_in": ", ".join(PREFERRED),
        }

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
        from core.models import SiteConfig
        from envelopes.services.posting import column_catalog

        if request.POST.get("action") == "reset":
            cfg = SiteConfig.get()
            cfg.envelope_default_funds = ""
            cfg.save(update_fields=["envelope_default_funds"])
            messages.success(request, "Restored the built-in envelope columns.")
            return redirect("envelope_columns")

        valid = {c["key"] for c in column_catalog()}
        keys, seen = [], set()
        for k in request.POST.getlist("columns"):
            if k in valid and k not in seen:
                seen.add(k)
                keys.append(k)
        if not keys:
            # An empty sheet is not a configuration, it is a mistake — and one
            # nobody would notice until the next Sabbath's entry.
            messages.error(request, "Choose at least one column for a new sheet.")
            return render(request, self.template_name, self._context())

        cfg = SiteConfig.get()
        cfg.envelope_default_funds = "\n".join(keys)
        cfg.save(update_fields=["envelope_default_funds"])
        messages.success(
            request, f"New envelope sheets will open with {len(keys)} "
                     f"column{'' if len(keys) == 1 else 's'}. Sheets already "
                     f"started are unchanged.")
        return redirect("envelope_columns")
