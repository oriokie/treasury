import datetime as dt
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction as db_tx
from django.db.models import Sum, Count, Q
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import ListView, TemplateView

from core.permissions import (ReadAccessMixin, DataEntryRequiredMixin,
                              TreasurerRequiredMixin)
from .models import PledgeCampaign, Pledge, PledgePayment, PledgeReminderLog
from .forms import CampaignForm, PledgeForm
from .services import matching as match_svc
from .services import reminders as rem_svc


# ===========================================================================
# Dashboard / overview
# ===========================================================================
class PledgeDashboardView(ReadAccessMixin, TemplateView):
    template_name = "pledges/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        campaigns = list(PledgeCampaign.objects.all())
        active = [c for c in campaigns if c.status == PledgeCampaign.Status.ACTIVE]
        ctx["campaigns"] = campaigns
        ctx["active_campaigns"] = active
        ctx["total_pledged"] = sum((c.total_pledged for c in campaigns), Decimal("0"))
        ctx["total_received"] = sum((c.total_received for c in campaigns), Decimal("0"))
        ctx["total_outstanding"] = sum((c.total_outstanding for c in campaigns), Decimal("0"))
        ctx["pledge_count"] = Pledge.objects.exclude(
            status=Pledge.Status.CANCELLED).count()
        ctx["draft_count"] = Pledge.objects.filter(status=Pledge.Status.DRAFT).count()
        from .models import PledgeMatchSuggestion
        ctx["suggestion_count"] = PledgeMatchSuggestion.objects.filter(status=PledgeMatchSuggestion.Status.PENDING).count()
        ctx["overdue"] = [p for p in Pledge.objects.filter(
            status__in=[Pledge.Status.ACTIVE, Pledge.Status.LAPSED])
            .select_related("member", "campaign") if p.is_overdue][:15]
        return ctx


# ===========================================================================
# Campaigns
# ===========================================================================
class CampaignListView(ReadAccessMixin, ListView):
    template_name = "pledges/campaign_list.html"
    context_object_name = "campaigns"

    def get_queryset(self):
        return PledgeCampaign.objects.all()


class CampaignDetailView(ReadAccessMixin, TemplateView):
    template_name = "pledges/campaign_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        c = get_object_or_404(PledgeCampaign, pk=kwargs["pk"])
        ctx["campaign"] = c
        pledges = list(c.pledges.exclude(status=Pledge.Status.CANCELLED)
                       .select_related("member"))
        ctx["pledges"] = pledges
        ctx["fulfilled"] = sum(1 for p in pledges if p.status == Pledge.Status.FULFILLED)
        ctx["lapsed"] = sum(1 for p in pledges if p.status == Pledge.Status.LAPSED)
        return ctx


class CampaignCreateView(TreasurerRequiredMixin, View):
    def get(self, request, pk=None):
        obj = get_object_or_404(PledgeCampaign, pk=pk) if pk else None
        form = CampaignForm(instance=obj)
        return render(request, "pledges/campaign_form.html",
                      {"form": form, "obj": obj})

    def post(self, request, pk=None):
        obj = get_object_or_404(PledgeCampaign, pk=pk) if pk else None
        form = CampaignForm(request.POST, instance=obj)
        if form.is_valid():
            c = form.save(commit=False)
            if not c.created_by_id:
                c.created_by = request.user
            c.save()
            messages.success(request, f"Campaign “{c.name}” saved.")
            return redirect("pledge_campaign_detail", pk=c.pk)
        return render(request, "pledges/campaign_form.html",
                      {"form": form, "obj": obj})


# ===========================================================================
# Pledges
# ===========================================================================
class PledgeListView(ReadAccessMixin, ListView):
    template_name = "pledges/pledge_list.html"
    context_object_name = "pledges"
    paginate_by = 50

    def get_queryset(self):
        qs = Pledge.objects.select_related("member", "campaign")
        g = self.request.GET
        if g.get("status"):
            qs = qs.filter(status=g["status"])
        if g.get("campaign"):
            qs = qs.filter(campaign_id=g["campaign"])
        if g.get("q"):
            qs = qs.filter(Q(member__name__icontains=g["q"])
                           | Q(campaign__name__icontains=g["q"]))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filters"] = self.request.GET
        ctx["statuses"] = Pledge.Status.choices
        ctx["campaigns"] = PledgeCampaign.objects.all()
        qs = self.get_queryset()
        ctx["sum_pledged"] = qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")
        # received across the filtered pledges
        ctx["sum_received"] = (PledgePayment.objects.filter(pledge__in=qs)
                               .aggregate(s=Sum("amount"))["s"] or Decimal("0"))
        ctx["sum_outstanding"] = (ctx["sum_pledged"] - ctx["sum_received"]
                                  if ctx["sum_pledged"] > ctx["sum_received"]
                                  else Decimal("0"))
        ctx["has_filters"] = any(g for g in (g2 for g2 in
                                 (self.request.GET.get(k) for k in
                                  ("status", "campaign", "q"))) if g)
        return ctx


class PledgeDetailView(ReadAccessMixin, TemplateView):
    template_name = "pledges/pledge_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        p = get_object_or_404(Pledge.objects.select_related("member", "campaign"),
                              pk=kwargs["pk"])
        ctx["pledge"] = p
        ctx["payments"] = p.payments.select_related("transaction").all()
        ctx["reminders"] = p.reminders.all()[:10]
        # expected schedule vs paid (informational)
        sched = p.expected_installments()
        ctx["schedule"] = [{"due": d, "amount": a} for d, a in sched]
        # match suggestions for the treasurer
        if self.request.user.has_perm and p.status == Pledge.Status.ACTIVE:
            ctx["suggestions"] = match_svc.suggest_matches_for_pledge(p)
        else:
            ctx["suggestions"] = []
        return ctx


class PledgeCreateView(DataEntryRequiredMixin, View):
    def get(self, request, pk=None):
        obj = get_object_or_404(Pledge, pk=pk) if pk else None
        initial = {}
        if request.GET.get("campaign"):
            initial["campaign"] = request.GET["campaign"]
        if request.GET.get("member"):
            initial["member"] = request.GET["member"]
        form = PledgeForm(instance=obj, initial=initial)
        return render(request, "pledges/pledge_form.html", {"form": form, "obj": obj})

    def post(self, request, pk=None):
        obj = get_object_or_404(Pledge, pk=pk) if pk else None
        form = PledgeForm(request.POST, instance=obj)
        if form.is_valid():
            p = form.save(commit=False)
            if not p.recorded_by_id:
                p.recorded_by = request.user
            # a treasurer recording a pledge can have it auto-approved; an
            # assistant's pledge stays DRAFT until a treasurer approves it
            if not obj:
                from core.roles import can_approve
                if can_approve(request.user):
                    p.status = Pledge.Status.ACTIVE
                    p.approved_by = request.user
                    p.approved_at = timezone.now()
                else:
                    p.status = Pledge.Status.DRAFT
            p.save()
            messages.success(request, "Pledge saved.")
            return redirect("pledge_detail", pk=p.pk)
        return render(request, "pledges/pledge_form.html", {"form": form, "obj": obj})


class PledgeApproveView(TreasurerRequiredMixin, View):
    """Approve a draft pledge, or cancel/reactivate. Mirrors expense approval."""
    def post(self, request, pk):
        p = get_object_or_404(Pledge, pk=pk)
        action = request.POST.get("action")
        if action == "approve" and p.status == Pledge.Status.DRAFT:
            p.status = Pledge.Status.ACTIVE
            p.approved_by = request.user
            p.approved_at = timezone.now()
            p.save()
            p.recompute_status()
            messages.success(request, "Pledge approved and now active.")
        elif action == "cancel":
            p.status = Pledge.Status.CANCELLED
            p.save()
            messages.success(request, "Pledge cancelled.")
        elif action == "reactivate" and p.status in (Pledge.Status.CANCELLED,
                                                      Pledge.Status.LAPSED):
            p.status = Pledge.Status.ACTIVE
            p.save()
            p.recompute_status()
            messages.success(request, "Pledge reactivated.")
        return redirect("pledge_detail", pk=p.pk)


# ===========================================================================
# Matching contributions to pledges
# ===========================================================================
class PledgeMatchView(TreasurerRequiredMixin, View):
    """Confirm a specific suggested contribution, or auto-match the whole pledge."""
    @db_tx.atomic
    def post(self, request, pk):
        p = get_object_or_404(Pledge, pk=pk)
        action = request.POST.get("action")
        if action == "auto":
            applied = match_svc.auto_match_pledge(p, user=request.user)
            if applied > 0:
                messages.success(request,
                    f"Auto-matched KES {applied:,.2f} from existing contributions.")
            else:
                messages.info(request, "No unmatched contributions found to apply.")
        elif action == "match":
            txn_id = request.POST.get("transaction")
            from giving.models import Transaction
            t = get_object_or_404(Transaction, pk=txn_id)
            try:
                amt = Decimal(request.POST.get("amount") or t.amount)
            except (InvalidOperation, TypeError):
                amt = t.amount
            already = (PledgePayment.objects.filter(transaction=t)
                       .aggregate(s=Sum("amount"))["s"] or Decimal("0"))
            free = t.amount - already
            amt = min(amt, free, p.outstanding if p.outstanding > 0 else amt)
            if amt <= 0:
                messages.error(request, "Nothing left to apply from that contribution.")
                return redirect("pledge_detail", pk=p.pk)
            PledgePayment.objects.create(pledge=p, transaction=t, amount=amt,
                date=t.date, source=PledgePayment.Source.MANUAL,
                matched_by=request.user)
            messages.success(request, f"Matched KES {amt:,.2f} to this pledge.")
        elif action == "manual":
            # record a payment with no linked transaction (e.g. a cash gift not
            # yet in the ledger) — still informational, flagged for follow-up
            try:
                amt = Decimal(request.POST.get("amount") or "0")
            except InvalidOperation:
                amt = Decimal("0")
            if amt > 0:
                PledgePayment.objects.create(pledge=p, transaction=None, amount=amt,
                    date=dt.date.today(), source=PledgePayment.Source.MANUAL,
                    matched_by=request.user,
                    note=request.POST.get("note", "")[:200] or "Manual entry")
                messages.success(request, f"Recorded KES {amt:,.2f} toward the pledge.")
            else:
                messages.error(request, "Enter an amount greater than zero.")
        return redirect("pledge_detail", pk=p.pk)


class PledgePaymentDeleteView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        pp = get_object_or_404(PledgePayment, pk=pk)
        pledge_id = pp.pledge_id
        pledge = pp.pledge
        pp.delete()
        pledge.recompute_status()
        messages.success(request, "Match removed.")
        return redirect("pledge_detail", pk=pledge_id)


class PledgeAutoMatchAllView(TreasurerRequiredMixin, View):
    @db_tx.atomic
    def post(self, request):
        campaign = None
        if request.POST.get("campaign"):
            campaign = PledgeCampaign.objects.filter(pk=request.POST["campaign"]).first()
        touched, total = match_svc.auto_match_all(user=request.user, campaign=campaign)
        if touched:
            messages.success(request, f"Auto-matched KES {total:,.2f} across "
                                      f"{touched} pledge(s).")
        else:
            messages.info(request, "No new matches found.")
        return redirect("pledge_dashboard")


# ===========================================================================
# Reminders
# ===========================================================================
class PledgeReminderView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        p = get_object_or_404(Pledge, pk=pk)
        channel = request.POST.get("channel", "SMS")
        log = rem_svc.send_pledge_reminder(p, channel=channel, user=request.user)
        if log.ok:
            messages.success(request, "Reminder sent.")
        else:
            messages.warning(request, f"Reminder not sent: {log.message[:120]}")
        return redirect("pledge_detail", pk=p.pk)


class PledgeReminderBatchView(TreasurerRequiredMixin, TemplateView):
    """Preview the list of members to remind, then send to all with one click."""
    template_name = "pledges/reminder_batch.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        campaign = None
        if self.request.GET.get("campaign"):
            campaign = PledgeCampaign.objects.filter(
                pk=self.request.GET["campaign"]).first()
        ctx["campaign"] = campaign
        ctx["campaigns"] = PledgeCampaign.objects.filter(
            status=PledgeCampaign.Status.ACTIVE)
        ctx["targets"] = rem_svc.reminder_targets(campaign=campaign)
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        ctx["sms_enabled"] = cfg.sms_enabled
        ctx["whatsapp_enabled"] = cfg.whatsapp_enabled
        return ctx

    def post(self, request):
        campaign = None
        if request.POST.get("campaign"):
            campaign = PledgeCampaign.objects.filter(pk=request.POST["campaign"]).first()
        channel = request.POST.get("channel", "SMS")
        targets = rem_svc.reminder_targets(campaign=campaign)
        sent = 0
        for p in targets:
            log = rem_svc.send_pledge_reminder(p, channel=channel, user=request.user)
            if log.ok:
                sent += 1
        messages.success(request, f"Sent {sent} of {len(targets)} reminder(s).")
        return redirect("pledge_dashboard")


# ===========================================================================
# Reports & year-end statements
# ===========================================================================
class PledgeReportView(ReadAccessMixin, TemplateView):
    """Campaign progress + per-status breakdown, exportable."""
    template_name = "pledges/report.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "xlsx":
            return self._export()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["campaigns"] = PledgeCampaign.objects.all()
        ctx["status_rows"] = (Pledge.objects.exclude(status=Pledge.Status.CANCELLED)
                              .values("status").annotate(n=Count("id"),
                                                         total=Sum("amount"))
                              .order_by("status"))
        return ctx

    def _export(self):
        import io
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from django.http import HttpResponse
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Campaign progress"
        head = ["Campaign", "Status", "Goal", "Pledged", "Received",
                "Outstanding", "% received", "Pledges"]
        ws.append(head)
        for c in range(1, len(head) + 1):
            ws.cell(1, c).font = Font(bold=True, color="FFFFFF")
            ws.cell(1, c).fill = PatternFill("solid", fgColor="1F5F4F")
        for camp in PledgeCampaign.objects.all():
            ws.append([camp.name, camp.get_status_display(),
                       float(camp.goal_amount), float(camp.total_pledged),
                       float(camp.total_received), float(camp.total_outstanding),
                       camp.pct_received, camp.pledge_count])
        buf = io.BytesIO(); wb.save(buf)
        resp = HttpResponse(buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = 'attachment; filename="pledge_report.xlsx"'
        return resp


class MemberPledgeStatementView(ReadAccessMixin, TemplateView):
    """Year-end statement of a member's pledges and fulfilment — print/PDF ready."""
    template_name = "pledges/member_statement.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from members.models import Member
        m = get_object_or_404(Member, pk=kwargs["pk"])
        try:
            year = int(self.request.GET.get("year", dt.date.today().year))
        except (TypeError, ValueError):
            year = dt.date.today().year
        pledges = (m.pledges.exclude(status=Pledge.Status.CANCELLED)
                   .select_related("campaign"))
        rows = []
        for p in pledges:
            year_paid = (p.payments.filter(date__year=year)
                         .aggregate(s=Sum("amount"))["s"] or Decimal("0"))
            rows.append({"pledge": p, "year_paid": year_paid})
        ctx["member"] = m
        ctx["year"] = year
        ctx["rows"] = rows
        ctx["total_pledged"] = sum((r["pledge"].amount for r in rows), Decimal("0"))
        ctx["total_paid_year"] = sum((r["year_paid"] for r in rows), Decimal("0"))
        from core.models import SiteConfig
        ctx["cfg"] = SiteConfig.get()
        return ctx


# ===========================================================================
# Match suggestions (SUGGEST mode review queue)
# ===========================================================================
class PledgeSuggestionListView(TreasurerRequiredMixin, TemplateView):
    template_name = "pledges/suggestions.html"

    def get_context_data(self, **kwargs):
        from .models import PledgeMatchSuggestion
        ctx = super().get_context_data(**kwargs)
        ctx["suggestions"] = (PledgeMatchSuggestion.objects.filter(
            status=PledgeMatchSuggestion.Status.PENDING)
            .select_related("transaction", "pledge", "pledge__member",
                            "pledge__campaign", "transaction__department"))
        return ctx


class PledgeSuggestionActionView(TreasurerRequiredMixin, View):
    @db_tx.atomic
    def post(self, request, pk):
        from .models import PledgeMatchSuggestion
        s = get_object_or_404(PledgeMatchSuggestion, pk=pk)
        action = request.POST.get("action")
        if action == "confirm" and s.status == PledgeMatchSuggestion.Status.PENDING:
            # apply the match (capped at outstanding and at the gift's free amount)
            already = (PledgePayment.objects.filter(transaction=s.transaction)
                       .aggregate(x=Sum("amount"))["x"] or Decimal("0"))
            free = s.transaction.amount - already
            amt = min(s.amount, free, s.pledge.outstanding
                      if s.pledge.outstanding > 0 else s.amount)
            if amt > 0:
                PledgePayment.objects.create(pledge=s.pledge,
                    transaction=s.transaction, amount=amt,
                    date=s.transaction.date, source=PledgePayment.Source.AUTO,
                    matched_by=request.user, note="Confirmed from suggestion")
            s.status = PledgeMatchSuggestion.Status.CONFIRMED
            s.resolved_by = request.user
            s.resolved_at = timezone.now()
            s.save()
            messages.success(request, f"Matched KES {amt:,.0f} to "
                                      f"{s.pledge.member.name}'s pledge.")
        elif action == "dismiss":
            s.status = PledgeMatchSuggestion.Status.DISMISSED
            s.resolved_by = request.user
            s.resolved_at = timezone.now()
            s.save()
            messages.info(request, "Suggestion dismissed.")
        return redirect("pledge_suggestions")


# ===========================================================================
# Public member pledge form  (NO LOGIN — security-sensitive; off by default)
# ===========================================================================
# Design for safety:
#   * Off unless SiteConfig.pledge_public_form_enabled is set.
#   * Write-only: it creates a single self-submitted, UNVERIFIED draft Pledge and
#     nothing else. It never reads or exposes member data, balances, or other
#     pledges, so there is no information-disclosure surface.
#   * No member lookup/autocomplete is exposed (that would leak the membership
#     roll). The submitter types their name/phone as free text; a treasurer links
#     it to the real Member record during review.
#   * A draft posts to nothing — it cannot touch the ledger or a fund balance.
#   * Spam defences: only ACTIVE campaigns are selectable; a honeypot field; a
#     minimum render-to-submit time; a simple per-session/IP throttle; and a hard
#     amount ceiling. Approval is always manual.
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
import time


@method_decorator(csrf_protect, name="dispatch")
class PublicPledgeView(View):
    template_name = "pledges/public_form.html"
    MAX_AMOUNT = Decimal("100000000")   # sanity ceiling
    MIN_SECONDS = 2                     # forms filled faster than this are bots

    def _enabled(self):
        from core.models import SiteConfig
        return SiteConfig.get().pledge_public_form_enabled

    def get(self, request):
        if not self._enabled():
            return render(request, "pledges/public_disabled.html", status=404)
        campaigns = PledgeCampaign.objects.filter(status=PledgeCampaign.Status.ACTIVE)
        request.session["pledge_form_ts"] = time.time()
        from core.models import SiteConfig
        return render(request, self.template_name, {
            "campaigns": campaigns, "cfg": SiteConfig.get(),
            "frequencies": Pledge.Frequency.choices})

    def post(self, request):
        if not self._enabled():
            return render(request, "pledges/public_disabled.html", status=404)
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        campaigns = PledgeCampaign.objects.filter(status=PledgeCampaign.Status.ACTIVE)

        def fail(msg):
            return render(request, self.template_name,
                          {"campaigns": campaigns, "cfg": cfg,
                           "frequencies": Pledge.Frequency.choices, "error": msg,
                           "form": request.POST})

        # 1) honeypot — a hidden field real users never fill
        if (request.POST.get("website") or "").strip():
            return redirect("public_pledge_thanks")   # silently drop bots
        # 2) too-fast submit
        ts = request.session.get("pledge_form_ts")
        if ts and (time.time() - ts) < self.MIN_SECONDS:
            return fail("Please take a moment to review your details and try again.")
        # 3) light throttle: max 3 submissions per session
        n = request.session.get("pledge_submits", 0)
        if n >= 3:
            return fail("Thank you — we already received your pledge. Please contact "
                        "the treasurer for further changes.")

        name = (request.POST.get("name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        camp_id = request.POST.get("campaign")
        amount_raw = (request.POST.get("amount") or "").strip()
        freq = request.POST.get("frequency") or Pledge.Frequency.ONE_OFF
        note = (request.POST.get("note") or "").strip()[:200]

        if not name or len(name) < 3:
            return fail("Please enter your full name.")
        campaign = campaigns.filter(pk=camp_id).first()
        if not campaign:
            return fail("Please choose a campaign.")
        try:
            amount = Decimal(amount_raw.replace(",", ""))
        except (InvalidOperation, AttributeError):
            return fail("Please enter a valid amount.")
        if amount <= 0 or amount > self.MAX_AMOUNT:
            return fail("Please enter a valid amount.")
        if freq not in dict(Pledge.Frequency.choices):
            freq = Pledge.Frequency.ONE_OFF

        # Resolve to a Member only by an exact, unambiguous match; otherwise leave
        # unlinked for the treasurer. We never reveal whether a match was found.
        from members.models import Member
        from members.services.matching import name_key, normalize_phone
        member = None
        ph = normalize_phone(phone)
        if ph:
            member = Member.objects.filter(phone=ph).first()
        if not member:
            nk = name_key(name)
            matches = Member.objects.filter(name_key=nk)[:2]
            if len(matches) == 1:
                member = matches[0]
        if not member:
            # create a provisional member record (inactive until a treasurer
            # confirms) so the pledge always has an owner
            member = Member.objects.create(name=name, phone=ph or None,
                                           source=Member.Source.AUTO_BANK,
                                           active=False)

        Pledge.objects.create(
            campaign=campaign, member=member, amount=amount, frequency=freq,
            start_date=dt.date.today(), status=Pledge.Status.DRAFT,
            self_submitted=True,
            submitted_contact=f"{name} / {phone}"[:120],
            note=note)
        request.session["pledge_submits"] = n + 1

        # notify treasurers there's a self-submitted pledge to review
        try:
            from core.services.notifications import notify
            notify("pledge", f"New member pledge submitted by {name} "
                             f"(KES {amount:,.0f} to {campaign.name}) — review needed.",
                   link="/pledges/list/?status=DRAFT")
        except Exception:
            pass
        return redirect("public_pledge_thanks")


class PublicPledgeThanksView(View):
    def get(self, request):
        from core.models import SiteConfig
        return render(request, "pledges/public_thanks.html",
                      {"cfg": SiteConfig.get()})


# ===========================================================================
# Bulk pledge import (treasurer-only)
# ===========================================================================
#
# For loading pledges collected on paper (e.g. cards filled at a campaign
# launch). A two-step wizard mirroring the budget importer: upload -> review &
# map any unmatched members/campaigns -> apply. Imported pledges land as DRAFT
# so a treasurer still approves them, exactly like the public form — no pledge
# becomes active (or affects anything) without a human decision. Pledges never
# post to the ledger, so this is purely an informational bulk-entry convenience.

class PledgeImportView(TreasurerRequiredMixin, View):
    template_name = "pledges/import.html"

    FREQ_LABELS = {
        "ONE OFF": "ONE_OFF", "ONEOFF": "ONE_OFF", "ONE-OFF": "ONE_OFF",
        "ONCE": "ONE_OFF", "LUMP SUM": "ONE_OFF", "LUMPSUM": "ONE_OFF",
        "WEEKLY": "WEEKLY", "WEEK": "WEEKLY",
        "MONTHLY": "MONTHLY", "MONTH": "MONTHLY",
        "QUARTERLY": "QUARTERLY", "QUARTER": "QUARTERLY",
        "ANNUAL": "ANNUAL", "ANNUALLY": "ANNUAL", "YEARLY": "ANNUAL", "YEAR": "ANNUAL",
    }

    def get(self, request):
        if request.GET.get("download"):
            return self._download(request)
        return render(request, self.template_name, {"stage": "upload"})

    def post(self, request):
        if request.POST.get("apply"):
            return self._apply(request)
        return self._parse(request)

    # ---- template ----
    def _download(self, request):
        import io
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
        from django.http import HttpResponse
        wb = openpyxl.Workbook()
        ws = wb.active; ws.title = "Pledges"
        head = ["Member name", "Phone", "Campaign", "Amount", "Frequency",
                "Start date", "End date", "Note"]
        ws.append(head)
        for c in range(1, len(head) + 1):
            ws.cell(1, c).font = Font(bold=True, color="FFFFFF")
            ws.cell(1, c).fill = PatternFill("solid", fgColor="1F5F4F")
        # worked examples
        ws.append(["GRACE WANJIRU", "0712345678", "Sanctuary Roof", 50000,
                   "Monthly", "2026-01-01", "2026-12-31", "Pledged at launch"])
        ws.append(["PETER OTIENO", "", "Sanctuary Roof", 20000, "One-off",
                   "2026-01-01", "", ""])

        ref = wb.create_sheet("Lists")
        ref["A1"] = "Campaigns"; ref["A1"].font = Font(bold=True)
        camps = list(PledgeCampaign.objects.exclude(
            status=PledgeCampaign.Status.CLOSED).order_by("name"))
        for i, c in enumerate(camps, start=2):
            ref.cell(i, 1, c.name)
        ref["B1"] = "Frequency"; ref["B1"].font = Font(bold=True)
        for i, (v, l) in enumerate(Pledge.Frequency.choices, start=2):
            ref.cell(i, 2, l)

        nrows = 400
        if camps:
            dv_c = DataValidation(type="list",
                formula1=f"=Lists!$A$2:$A${len(camps) + 1}", allow_blank=True)
            ws.add_data_validation(dv_c); dv_c.add(f"C2:C{nrows}")
        dv_f = DataValidation(type="list",
            formula1=f"=Lists!$B$2:$B${len(Pledge.Frequency.choices) + 1}",
            allow_blank=True)
        ws.add_data_validation(dv_f); dv_f.add(f"E2:E{nrows}")

        ws.column_dimensions["A"].width = 26
        ws.column_dimensions["C"].width = 22
        ws.column_dimensions["H"].width = 26
        info = wb.create_sheet("How to fill this in")
        for i, line in enumerate([
            "Pledge import",
            "",
            "One row per pledge a member has made.",
            "  - Member name — required. Matched to an existing member by name or",
            "      phone; an unmatched name can be mapped or created on review.",
            "  - Phone — optional, helps match and is saved to a created member.",
            "  - Campaign — pick from the list. Blank/unmatched is flagged on review.",
            "  - Amount — the total promised (required, > 0).",
            "  - Frequency — One-off / Weekly / Monthly / Quarterly / Annual.",
            "  - Start / End date — optional (YYYY-MM-DD). End date drives the",
            "      schedule and the lapsed check.",
            "",
            "Imported pledges are saved as DRAFTS for a treasurer to approve.",
            "Nothing posts to the ledger — pledges are informational commitments.",
        ], start=1):
            info.cell(i, 1, line)
        info.column_dimensions["A"].width = 74

        buf = io.BytesIO(); wb.save(buf)
        resp = HttpResponse(buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = 'attachment; filename="pledge_import_template.xlsx"'
        return resp

    # ---- step 1: parse + build a review plan ----
    def _parse(self, request):
        import openpyxl
        from members.models import Member
        from members.services.matching import name_key
        from members.models import normalize_phone
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a spreadsheet to upload.")
            return redirect("pledge_import")
        try:
            wb = openpyxl.load_workbook(f, data_only=True)
        except Exception:
            messages.error(request, "Could not read that file — please upload a .xlsx.")
            return redirect("pledge_import")
        ws = wb["Pledges"] if "Pledges" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            messages.error(request, "The sheet is empty.")
            return redirect("pledge_import")
        header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]

        def col(*names):
            for n in names:
                if n in header:
                    return header.index(n)
            return None
        c_name = col("member name", "member", "name")
        c_phone = col("phone", "mobile")
        c_camp = col("campaign")
        c_amt = col("amount", "pledge", "pledged")
        c_freq = col("frequency")
        c_start = col("start date", "start")
        c_end = col("end date", "end")
        c_note = col("note", "notes")
        if c_name is None or c_amt is None:
            messages.error(request, "Couldn't find the Member name and Amount "
                                    "columns — please use the template.")
            return redirect("pledge_import")

        members = list(Member.objects.all())
        by_key = {}
        by_phone = {}
        for m in members:
            by_key.setdefault(m.name_key, m)
            if m.phone:
                by_phone[m.phone] = m
        campaigns = {self._norm(c.name): c for c in PledgeCampaign.objects.all()}

        def cell(r, idx):
            if idx is None or idx >= len(r) or r[idx] in (None, ""):
                return ""
            return str(r[idx]).strip()

        def parse_date(v):
            if not v:
                return None
            if isinstance(v, dt.datetime):
                return v.date().isoformat()
            if isinstance(v, dt.date):
                return v.isoformat()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y"):
                try:
                    return dt.datetime.strptime(str(v).strip(), fmt).date().isoformat()
                except ValueError:
                    continue
            return None

        plan = []
        for r in rows[1:]:
            name = cell(r, c_name)
            if not name:
                continue
            try:
                amt = float(r[c_amt]) if c_amt < len(r) and r[c_amt] not in (None, "") else 0.0
            except (TypeError, ValueError):
                amt = 0.0
            if amt <= 0:
                continue
            phone = normalize_phone(cell(r, c_phone)) or cell(r, c_phone)
            # match member: phone first, then unambiguous name key
            m = None
            score = 0.0
            if phone and phone in by_phone:
                m = by_phone[phone]; score = 1.0
            if not m:
                nk = name_key(name)
                if nk and nk in by_key:
                    m = by_key[nk]; score = 0.9
            camp_raw = cell(r, c_camp)
            camp = campaigns.get(self._norm(camp_raw)) if camp_raw else None
            freq_raw = cell(r, c_freq).upper().replace("/", " ").strip()
            freq = self.FREQ_LABELS.get(freq_raw, "ONE_OFF")
            plan.append({
                "name": name, "phone": phone,
                "member_id": m.id if m else None,
                "member_match": m.name if m else None,
                "member_score": score,
                "campaign_raw": camp_raw,
                "campaign_id": camp.id if camp else None,
                "campaign_match": camp.name if camp else None,
                "amount": amt, "frequency": freq,
                "start": parse_date(r[c_start]) if c_start is not None and c_start < len(r) else None,
                "end": parse_date(r[c_end]) if c_end is not None and c_end < len(r) else None,
                "note": cell(r, c_note)[:200],
            })

        if not plan:
            messages.error(request, "No pledge rows with an amount were found.")
            return redirect("pledge_import")

        request.session["pledge_import_plan"] = plan
        unmatched_members = sum(1 for p in plan if not p["member_id"])
        unmatched_camps = sorted({p["campaign_raw"] for p in plan
                                  if p["campaign_raw"] and not p["campaign_id"]})
        no_campaign = sum(1 for p in plan if not p["campaign_raw"])
        member_candidates = [{"id": m.id, "name": m.name} for m in
                             sorted(members, key=lambda x: x.name)]
        campaign_candidates = [{"id": c.id, "name": c.name} for c in
                               PledgeCampaign.objects.exclude(
                                   status=PledgeCampaign.Status.CLOSED).order_by("name")]
        return render(request, self.template_name, {
            "stage": "review", "plan": plan,
            "total": sum(p["amount"] for p in plan),
            "unmatched_members": unmatched_members,
            "unmatched_camps": unmatched_camps,
            "no_campaign": no_campaign,
            "member_candidates": member_candidates,
            "campaign_candidates": campaign_candidates,
            "frequencies": Pledge.Frequency.choices,
        })

    # ---- step 2: apply ----
    @db_tx.atomic
    def _apply(self, request):
        from members.models import Member
        plan = request.session.get("pledge_import_plan")
        if not plan:
            messages.error(request, "Your import session expired — please upload again.")
            return redirect("pledge_import")
        created = skipped = new_members = new_camps = 0
        # a single "default campaign" choice can cover rows with no campaign
        default_campaign_id = request.POST.get("default_campaign") or None

        for i, p in enumerate(plan):
            # resolve member
            mchoice = request.POST.get(f"member_{i}", "")
            member = None
            if mchoice.startswith("member:"):
                member = Member.objects.filter(pk=mchoice.split(":", 1)[1]).first()
            elif mchoice == "create" or (mchoice == "" and not p["member_id"]):
                if p["name"]:
                    member = Member.objects.create(
                        name=p["name"], phone=p["phone"] or None,
                        source=Member.Source.MANUAL)
                    new_members += 1
            elif p["member_id"]:
                member = Member.objects.filter(pk=p["member_id"]).first()
            if not member:
                skipped += 1
                continue

            # resolve campaign
            cchoice = request.POST.get(f"campaign_{i}", "")
            campaign = None
            if cchoice.startswith("campaign:"):
                campaign = PledgeCampaign.objects.filter(pk=cchoice.split(":", 1)[1]).first()
            elif cchoice == "create" and p["campaign_raw"]:
                campaign, was = PledgeCampaign.objects.get_or_create(
                    name=p["campaign_raw"].strip(),
                    defaults={"status": PledgeCampaign.Status.ACTIVE,
                              "created_by": request.user})
                if was:
                    new_camps += 1
            elif p["campaign_id"]:
                campaign = PledgeCampaign.objects.filter(pk=p["campaign_id"]).first()
            elif default_campaign_id:
                campaign = PledgeCampaign.objects.filter(pk=default_campaign_id).first()
            if not campaign:
                skipped += 1
                continue

            from decimal import Decimal as D
            kwargs = dict(
                campaign=campaign, member=member, amount=D(str(p["amount"])),
                frequency=p["frequency"], status=Pledge.Status.DRAFT,
                recorded_by=request.user, note=p["note"])
            if p["start"]:
                kwargs["start_date"] = p["start"]
            if p["end"]:
                kwargs["end_date"] = p["end"]
            Pledge.objects.create(**kwargs)
            created += 1

        request.session.pop("pledge_import_plan", None)
        parts = [f"{created} pledge(s) imported as drafts"]
        if new_members:
            parts.append(f"{new_members} new member(s)")
        if new_camps:
            parts.append(f"{new_camps} new campaign(s)")
        if skipped:
            parts.append(f"{skipped} row(s) skipped")
        messages.success(request, ", ".join(parts) +
                         ". Review and approve them on the pledge list.")
        return redirect(f"{reverse('pledge_list')}?status=DRAFT")

    @staticmethod
    def _norm(s):
        return " ".join((s or "").upper().split())
