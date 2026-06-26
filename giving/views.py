from decimal import Decimal, InvalidOperation
from django.db import transaction as db_tx

def _cash_duplicate(d, dept, amount, name=None):
    """True if a near-identical manual cash entry already exists. A duplicate must
    match the fund and amount (within +/-1 day) AND have a similar payer name
    (>50% match) — matching on amount alone wrongly flags different givers who
    happen to give the same amount on the same day."""
    if not (d and dept and amount):
        return False
    import datetime as _dt
    from difflib import SequenceMatcher
    from members.models import name_key
    from giving.models import Transaction
    cands = Transaction.objects.filter(
        channel=Transaction.Channel.CASH, department=dept, amount=amount,
        date__range=(d - _dt.timedelta(days=1), d + _dt.timedelta(days=1)))
    new_key = name_key(name or "")
    if not new_key:
        return False        # no name to compare -> don't guess it's a duplicate
    for t in cands:
        other = name_key(t.payer_name or "")
        if not other:
            continue
        if SequenceMatcher(None, new_key, other).ratio() > 0.5:
            return True
    return False


from core.utils import block_if_locked as _block_if_locked
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import ListView, CreateView, View, DeleteView

from core.permissions import DataEntryRequiredMixin, ReadAccessMixin, TreasurerRequiredMixin
from core.utils import sabbath_week_of
from departments.models import Department
from .models import Transaction, AllocationRule
from .forms import CashEntryForm, QueueResolveForm, RuleForm
from .services.allocation import normalize_reference


class TransactionListView(ReadAccessMixin, ListView):
    model = Transaction
    template_name = "giving/transaction_list.html"
    context_object_name = "transactions"
    paginate_by = 50

    def get(self, request, *args, **kwargs):
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            qs = self.get_queryset()
            header = ["Date", "Sabbath", "Channel", "Direction", "Payer", "Member",
                      "Phone", "Fund", "Dev group", "Reference", "M-Pesa ref",
                      "Core ref", "Bank receipt", "Receipt status", "Status",
                      "Confirmed", "Amount"]

            def _receipt_status(t):
                if t.processed_via_envelope or t.channel == Transaction.Channel.ENVELOPE:
                    return "Receipted (envelope)"
                if t.manual_receipt:
                    return "Receipted (manual)"
                if t.excluded_from_income:
                    return "Memo (reconciled to envelope)"
                return "Not receipted"

            rows = [[t.date.isoformat(),
                     t.service_sabbath.isoformat() if t.service_sabbath else "",
                     t.get_channel_display(), t.get_direction_display(),
                     t.payer_name or (t.member.name if t.member else ""),
                     t.member.name if t.member_id else "",
                     t.payer_phone or (t.member.receipt_phone if t.member_id else "") or "",
                     t.department.name if t.department else
                     ("Excluded (via envelope)" if t.excluded_from_income else "Unallocated"),
                     t.dev_group.label if t.dev_group_id else "",
                     t.reference or "", t.mpesa_ref or "", t.core_ref or "",
                     t.bank_receipt or "", _receipt_status(t),
                     t.get_allocation_status_display(),
                     "Yes" if t.confirmed else "",
                     float(t.amount if t.direction == "CREDIT" else -t.amount)]
                    for t in qs]
            if export == "xlsx":
                return xlsx_response("transactions.xlsx", header, rows,
                                     title="Transactions", church=SiteConfig.get().church_name)
            return csv_response("transactions.csv", header, rows)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        qs = (Transaction.objects.select_related("department", "member", "dev_group")
              .order_by("-date", "-id"))
        q = self.request.GET.get("q")
        channel = self.request.GET.get("channel")
        status = self.request.GET.get("status")
        dept = self.request.GET.get("department")
        if q:
            cond = (Q(payer_name__icontains=q) | Q(reference__icontains=q) |
                    Q(core_ref__icontains=q) | Q(raw_narration__icontains=q) |
                    Q(mpesa_ref__icontains=q) | Q(bank_receipt__icontains=q))
            # let the same box find an entry by its amount, e.g. "250" or "1,250.50"
            try:
                qstr = q.replace(",", "").strip()
                amt = Decimal(qstr)
                if "." in qstr:
                    cond |= Q(amount=amt)            # decimals: exact
                else:
                    cond |= Q(amount__gte=amt) & Q(amount__lt=amt + 1)  # "1234" finds 1234.x
            except (InvalidOperation, AttributeError):
                pass
            qs = qs.filter(cond)
        if channel:
            qs = qs.filter(channel=channel)
        if status:
            qs = qs.filter(allocation_status=status)
        if dept == "none":
            qs = qs.filter(department__isnull=True)
        elif dept:
            qs = qs.filter(department_id=dept)
        date_from = self.request.GET.get("date_from")
        date_to = self.request.GET.get("date_to")
        # parse defensively: an invalid date string would otherwise raise and
        # break the page; ignore anything that isn't a real YYYY-MM-DD date.
        from django.utils.dateparse import parse_date
        df = parse_date(date_from) if date_from else None
        dtv = parse_date(date_to) if date_to else None
        if df:
            qs = qs.filter(date__gte=df)
        if dtv:
            qs = qs.filter(date__lte=dtv)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["departments"] = Department.objects.filter(active=True, selectable=True)
        ctx["channels"] = Transaction.Channel.choices
        ctx["statuses"] = Transaction.Status.choices
        ctx["filters"] = self.request.GET
        ctx["unallocated_count"] = Transaction.objects.active().filter(
            department__isnull=True, excluded_from_income=False).count()
        ctx["noflund_total"] = Transaction.objects.active().filter(
            department__isnull=True).count()
        # summary stats for the filtered set (header cards)
        from django.db.models import Sum, Count, Q as _Q
        qs = self.get_queryset()
        agg = qs.aggregate(
            credits=Sum("amount", filter=_Q(direction="CREDIT")),
            debits=Sum("amount", filter=_Q(direction="DEBIT")),
            n=Count("id"),
            review=Count("id", filter=_Q(allocation_status="REVIEW")))
        ctx["sum_credits"] = agg["credits"] or 0
        ctx["sum_debits"] = agg["debits"] or 0
        ctx["sum_net"] = (agg["credits"] or 0) - (agg["debits"] or 0)
        ctx["sum_count"] = agg["n"] or 0
        ctx["sum_review"] = agg["review"] or 0
        ctx["has_filters"] = any(self.request.GET.get(k) for k in
                                 ("q", "channel", "status", "department",
                                  "date_from", "date_to"))
        return ctx


class ReviewQueueView(ReadAccessMixin, ListView):
    template_name = "giving/queue.html"
    context_object_name = "items"
    paginate_by = 25

    def get_queryset(self):
        # Only credits (giving) awaiting allocation belong here; bank debits with a
        # REVIEW status are handled in the separate bank-debit queue. Anything
        # already receipted (system envelope) or marked as a manual paper receipt
        # is handled and must not appear here.
        return (Transaction.objects.filter(
                    allocation_status=Transaction.Status.REVIEW,
                    direction=Transaction.Direction.CREDIT,
                    processed_via_envelope=False, manual_receipt=False)
                .select_related("member").order_by("date", "id"))

    def get_context_data(self, **kwargs):
        from departments.models import DevelopmentGroup
        from giving.models import SplitFund
        ctx = super().get_context_data(**kwargs)
        ctx["departments"] = Department.objects.filter(active=True, selectable=True)
        ctx["split_funds"] = SplitFund.objects.filter(active=True).order_by("name")
        ctx["dev_groups"] = DevelopmentGroup.objects.filter(active=True).order_by("number")
        # item 5: gifts in the ledger that still need a fund but aren't in the queue
        ctx["unallocated_in_ledger"] = FetchUnallocatedView.pending_qs().count()
        return ctx


class RunRulesOnQueueView(DataEntryRequiredMixin, View):
    """Re-run allocation rules over the items still in the review queue, so rules
    added after an import can clear matching items without re-importing the file."""

    def post(self, request):
        from giving.services.allocation import reallocate_pending
        r = reallocate_pending()
        if r["scanned"] == 0:
            messages.info(request, "The review queue is empty — nothing to allocate.")
        elif r["allocated"]:
            msg = (f"Allocated {r['allocated']} of {r['scanned']} item(s) using the "
                   f"current rules. {r['remaining']} still need attention.")
            extra = []
            if r["skipped_locked"]:
                extra.append(f"{r['skipped_locked']} in a locked period were skipped")
            if r["skipped_split"]:
                extra.append(f"{r['skipped_split']} matched a split fund (allocate manually)")
            if extra:
                msg += " (" + "; ".join(extra) + ")."
            messages.success(request, msg)
        else:
            note = ""
            if r["skipped_locked"]:
                note = f" {r['skipped_locked']} were in a locked period."
            messages.info(request, "No queued items matched the current rules — "
                                   "check the rule reference and match type." + note)
        return redirect("queue")


class BulkAllocateView(DataEntryRequiredMixin, View):
    """Item 1: allocate several review-queue contributions to one fund in a single action,
    for faster clearing of the queue. Optionally sets a development group when the
    chosen fund is a development fund."""

    def post(self, request):
        from departments.models import DevelopmentGroup
        ids = request.POST.getlist("txn")
        raw_dept = request.POST.get("department", "")
        if not ids:
            messages.error(request, "Pick a fund and at least one contribution to allocate.")
            return redirect("queue")

        base_qs = Transaction.objects.filter(
            id__in=ids, allocation_status=Transaction.Status.REVIEW,
            direction=Transaction.Direction.CREDIT,
            processed_via_envelope=False, manual_receipt=False)

        # --- split fund (e.g. Combined Offering = Trust + Local) -------------
        if raw_dept.startswith("sf:"):
            from giving.models import SplitFund
            sf = SplitFund.objects.filter(pk=raw_dept[3:], active=True).first()
            if not sf:
                messages.error(request, "That split fund is no longer available.")
                return redirect("queue")
            n = 0
            for txn in base_qs:
                parts = [(d, amt, None) for d, amt in sf.split(txn.amount)]
                try:
                    txn.split_into(parts, user=request.user)
                except (ValueError, ArithmeticError):
                    continue
                txn.claimed_by = request.user
                txn.claimed_at = timezone.now()
                txn.save(update_fields=["claimed_by", "claimed_at"])
                n += 1
            if n:
                messages.success(request, f"Allocated {n} contribution(s) to {sf.name} — "
                                          "each split into its parts; the trust "
                                          "portion is queued for receipting.")
            else:
                messages.info(request, "No matching contributions to allocate.")
            return redirect("queue")

        # --- ordinary fund ----------------------------------------------------
        dept = Department.objects.filter(pk=raw_dept, active=True).first()
        if not dept:
            messages.error(request, "Pick a fund and at least one contribution to allocate.")
            return redirect("queue")
        grp = None
        if dept.category == Department.Category.DEVELOPMENT:
            grp = DevelopmentGroup.objects.filter(pk=request.POST.get("dev_group")).first()
        n = 0
        for txn in base_qs:
            txn.department = dept
            txn.dev_group = grp if grp else None
            txn.allocation_status = Transaction.Status.MANUAL
            txn.claimed_by = request.user
            txn.claimed_at = timezone.now()
            txn.save(update_fields=["department", "dev_group", "allocation_status",
                                    "claimed_by", "claimed_at"])
            n += 1
        if n:
            label = dept.name + (f" · {grp.label}" if grp else "")
            messages.success(request, f"Allocated {n} contribution(s) to {label}.")
        else:
            messages.info(request, "No matching contributions to allocate.")
        return redirect("queue")


class FetchUnallocatedView(DataEntryRequiredMixin, View):
    """Item 5: pull credits that still need a fund — sitting in the ledger without
    a department but not currently in the review queue — into the queue so they
    can be allocated. A contribution can fall out of REVIEW (e.g. imported already
    'confirmed' but with no fund); this gathers them back for allocation."""

    @staticmethod
    def pending_qs():
        return Transaction.objects.filter(
            direction=Transaction.Direction.CREDIT, department__isnull=True,
            processed_via_envelope=False, manual_receipt=False,
            is_reversal=False, is_reversed=False,
            excluded_from_income=False).exclude(
            allocation_status=Transaction.Status.REVIEW)

    def post(self, request):
        n = self.pending_qs().update(allocation_status=Transaction.Status.REVIEW)
        if n:
            messages.success(request,
                f"Fetched {n} unallocated contribution(s) from the ledger into the queue.")
        else:
            messages.info(request, "No unallocated contributions found in the ledger.")
        return redirect("queue")


class ClaimResolveView(DataEntryRequiredMixin, View):
    """Claim + resolve a review item; optionally remember the rule."""

    def post(self, request, pk):
        txn = get_object_or_404(Transaction, pk=pk,
                                allocation_status=Transaction.Status.REVIEW,
                                direction=Transaction.Direction.CREDIT)
        # --- split allocation: one bank gift meant for several funds ---
        if request.POST.get("split") == "1":
            from departments.models import Department as _D, DevelopmentGroup as _G
            from giving.models import SplitFund as _SF
            from decimal import Decimal as _Dec
            parts = []
            grps = request.POST.getlist("split_grp")
            for n, (d_id, amt) in enumerate(zip(request.POST.getlist("split_dept"),
                                                request.POST.getlist("split_amount"))):
                if not d_id or not str(amt).strip():
                    continue
                # a split-fund target sub-divides this part across its components
                if str(d_id).startswith("sf:"):
                    sf = _SF.objects.filter(pk=d_id[3:], active=True).first()
                    if not sf:
                        continue
                    try:
                        row_amt = _Dec(str(amt))
                    except (ArithmeticError, ValueError):
                        continue
                    for sub_d, sub_amt in sf.split(row_amt):
                        parts.append((sub_d, sub_amt, None))
                    continue
                d = _D.objects.filter(pk=d_id, active=True).first()
                if not d:
                    continue
                grp = None
                if d.category == Department.Category.DEVELOPMENT and n < len(grps) and grps[n]:
                    grp = _G.objects.filter(pk=grps[n]).first()
                parts.append((d, amt, grp))
            try:
                txn.split_into(parts, user=request.user)
            except (ValueError, ArithmeticError) as e:
                messages.error(request, f"Could not split: {e}")
                return redirect("queue")
            txn.claimed_by = request.user
            txn.claimed_at = timezone.now()
            txn.save(update_fields=["claimed_by", "claimed_at"])
            messages.success(request,
                f"Split across {len(parts)} funds: " +
                ", ".join(f"{d.name} {a}" for d, a, _ in parts) + ".")
            return redirect("queue")

        # --- split fund (e.g. Combined Offering = 50% Trust + 50% Local) ---
        raw_dept = request.POST.get("department", "")
        if raw_dept.startswith("sf:"):
            from giving.models import SplitFund
            sf = SplitFund.objects.filter(pk=raw_dept[3:], active=True).first()
            if not sf:
                messages.error(request, "That split fund is no longer available.")
                return redirect("queue")
            parts = [(d, amt, None) for d, amt in sf.split(txn.amount)]
            try:
                txn.split_into(parts, user=request.user)
            except (ValueError, ArithmeticError) as e:
                messages.error(request, f"Could not split: {e}")
                return redirect("queue")
            txn.claimed_by = request.user
            txn.claimed_at = timezone.now()
            txn.save(update_fields=["claimed_by", "claimed_at"])
            if request.POST.get("remember_rule") and txn.reference:
                ref = normalize_reference(txn.reference)
                AllocationRule.objects.update_or_create(
                    reference=ref,
                    defaults={"split_fund": sf, "department": None,
                              "source": AllocationRule.Source.LEARNED})
            messages.success(request,
                f"Allocated to {sf.name} — split into " +
                ", ".join(f"{d.name} {a}" for d, a in sf.split(txn.amount)) +
                ". The trust portion is queued for receipting.")
            return redirect("queue")

        form = QueueResolveForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Pick a fund to allocate to.")
            return redirect("queue")

        dept = form.cleaned_data["department"]
        txn.department = dept
        if dept.category == Department.Category.DEVELOPMENT and request.POST.get("dev_group"):
            from departments.models import DevelopmentGroup as _G
            txn.dev_group = _G.objects.filter(pk=request.POST["dev_group"]).first()
        txn.allocation_status = Transaction.Status.MANUAL
        txn.claimed_by = request.user
        txn.claimed_at = timezone.now()
        txn.save()

        resolved_similar = 0
        if form.cleaned_data["remember_rule"] and txn.reference:
            ref = normalize_reference(txn.reference)
            AllocationRule.objects.update_or_create(
                reference=ref,
                defaults={"department": dept, "split_fund": None,
                          "source": AllocationRule.Source.LEARNED},
            )
            # apply to all other queued items with the same reference
            similar = Transaction.objects.filter(
                allocation_status=Transaction.Status.REVIEW,
                reference__iexact=txn.reference,
            ).exclude(pk=txn.pk)
            resolved_similar = similar.update(
                department=dept, allocation_status=Transaction.Status.LEARNED)

        msg = f"Allocated to {dept.name}."
        if resolved_similar:
            msg += f" Rule applied to {resolved_similar} similar item(s)."
        messages.success(request, msg)
        return redirect("queue")


class CashEntryListView(TransactionListView):
    """A focused, filterable table of cash collections (channel = CASH), so cash
    entries can be checked the same way envelopes are."""
    def get_queryset(self):
        return super().get_queryset().filter(channel="CASH",
                                             direction=Transaction.Direction.CREDIT)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cash_only"] = True
        return ctx


class CashEntryCreate(DataEntryRequiredMixin, CreateView):
    model = Transaction
    form_class = CashEntryForm
    template_name = "giving/cash_form.html"
    success_url = reverse_lazy("transaction_list")

    def form_valid(self, form):
        if _block_if_locked(self.request, form.cleaned_data.get("date")):
            return redirect("cash_new")
        # H3: guard against double-entering the same collection (e.g. once by the
        # assistant and once by the treasurer). Require explicit confirmation.
        if not self.request.POST.get("confirm_duplicate") and _cash_duplicate(
                form.cleaned_data.get("date"), form.cleaned_data.get("department"),
                form.cleaned_data.get("amount"), form.cleaned_data.get("payer_name")):
            messages.warning(self.request,
                "A cash entry for this fund, date and amount already exists. If this is a "
                "separate collection, tick \u201cThis is not a duplicate\u201d and save again.")
            ctx = self.get_context_data(form=form)
            ctx["duplicate_warning"] = True
            return self.render_to_response(ctx)
        split = form.split_fund
        if split:
            base = form.save(commit=False)
            from core.utils import sabbath_of as _sof
            _svc = _sof(base.date) if base.date else None
            for dept, amt in split.split(base.amount):
                Transaction.objects.create(
                    date=base.date, channel=base.channel,
                    direction=Transaction.Direction.CREDIT,
                    allocation_status=Transaction.Status.MANUAL,
                    sabbath_week=sabbath_week_of(base.date),
                    service_sabbath=_svc,
                    amount=amt, department=dept, member=base.member,
                    reference=base.reference, payer_name=base.payer_name)
            messages.success(self.request, f"Entry recorded and split across {split.name}.")
            return redirect(self.success_url)
        txn = form.save(commit=False)
        txn.direction = Transaction.Direction.CREDIT
        txn.allocation_status = Transaction.Status.MANUAL
        txn.sabbath_week = sabbath_week_of(txn.date)
        # the treasurer dated this cash to a specific Sabbath; honour it directly
        # rather than rolling a "closed" Sabbath forward (that roll is for bank
        # gifts that physically arrive after a Sabbath, not counted cash).
        from core.utils import sabbath_of as _sof
        if txn.date and txn.service_sabbath is None:
            txn.service_sabbath = _sof(txn.date)
        txn.save()
        # offer/apply a pledge match if this giver has an active pledge
        try:
            from pledges.services.matching import handle_new_contribution
            note = handle_new_contribution(txn, user=self.request.user)
        except Exception:
            from core.utils import log_exception as _lx; _lx('giving/views.py')
            note = None
        if note:
            messages.success(self.request, f"Entry recorded — {note}.")
        else:
            messages.success(self.request, "Entry recorded.")
        return redirect(self.success_url)


class RuleListView(DataEntryRequiredMixin, ListView):
    model = AllocationRule
    template_name = "giving/rule_list.html"
    context_object_name = "rules"
    paginate_by = 50

    def get_queryset(self):
        return (AllocationRule.objects.select_related("department", "split_fund")
                .order_by("reference", "id"))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["form"] = RuleForm()
        return ctx


class RuleCreateView(DataEntryRequiredMixin, CreateView):
    model = AllocationRule
    form_class = RuleForm
    template_name = "giving/rule_form.html"
    success_url = reverse_lazy("rule_list")

    def form_valid(self, form):
        form.instance.reference = normalize_reference(form.instance.reference)
        messages.success(self.request, "Rule saved.")
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, "That reference already has a rule.")
        return redirect("rule_list")


class RuleDeleteView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        from django.shortcuts import get_object_or_404
        rule = get_object_or_404(AllocationRule, pk=pk)
        ref = rule.reference
        rule.delete()
        messages.success(request, f"Deleted allocation rule “{ref}”.")
        return redirect("rule_list")

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)


from django.views.generic import UpdateView
from .forms import TransactionEditForm


class TransactionUpdateView(DataEntryRequiredMixin, UpdateView):
    model = Transaction
    form_class = TransactionEditForm
    template_name = "giving/transaction_form.html"

    def get_success_url(self):
        return reverse_lazy("transaction_list")

    def form_valid(self, form):
        # M6: block if either the original or the new date falls in a locked period
        original = type(self.object).objects.filter(pk=self.object.pk).values_list(
            "date", flat=True).first()
        if _block_if_locked(self.request, form.instance.date) or \
           (original and _block_if_locked(self.request, original)):
            return redirect("transaction_list")
        # did the "manual receipt" box change on this save? (reversible both ways)
        changed_manual = "manual_receipt" in form.changed_data
        new_value = form.instance.manual_receipt
        response = super().form_valid(form)
        if changed_manual:
            # marking on: pull it (and split siblings) out of the queues so it
            # isn't receipted again. Un-marking: clear the flag on the whole gift
            # so it becomes eligible for a system receipt once more.
            n = self.object.mark_manual_receipt(value=new_value, cascade_split=True)
            if new_value and n > 1:
                messages.success(self.request,
                    f"Marked {n} split parts as manual receipts and cleared them "
                    f"from the queue.")
            elif new_value:
                messages.success(self.request, "Marked as a manual receipt.")
            else:
                messages.success(self.request,
                    "Manual-receipt mark removed — this contribution can be receipted again.")
        else:
            messages.success(self.request, "Entry updated.")
        return response


# ---- Bank-statement debit handling ----
from django.shortcuts import render, get_object_or_404
from cashbook.models import Expense
from departments.models import Department
from core.utils import sabbath_week_of


def _float_fund():
    fund, _ = Department.objects.get_or_create(
        name="Float / Cash on hand",
        defaults=dict(fund_type=Department.FundType.LOCAL,
                      category=Department.Category.HOLDING))
    return fund


class DebitQueueView(DataEntryRequiredMixin, ListView):
    """Bank-statement debits awaiting classification."""
    template_name = "giving/debit_queue.html"
    context_object_name = "debits"
    paginate_by = 50

    def get_queryset(self):
        return (Transaction.objects.filter(
            direction=Transaction.Direction.DEBIT,
            channel=Transaction.Channel.BANK,
            allocation_status=Transaction.Status.REVIEW)
            .order_by("-date"))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["funds"] = Department.objects.filter(active=True, selectable=True)
        ctx["categories"] = Expense.Category.choices
        ctx["pending_expenses"] = Expense.objects.filter(
            status__in=[Expense.Status.PENDING, Expense.Status.APPROVED],
            bank_transaction__isnull=True).order_by("-date")[:200]
        return ctx


class DebitResolveView(DataEntryRequiredMixin, View):
    """Classify one debit as a bank charge, a general expense, a float
    withdrawal, or a match to an existing expense."""

    def post(self, request, pk):
        txn = get_object_or_404(
            Transaction, pk=pk, direction=Transaction.Direction.DEBIT)
        # a debit carries a date; resolving it posts an expense/transfer on that
        # date, so it must honour the period lock just like any other entry.
        if _block_if_locked(request, txn.date):
            return redirect("debit_queue")
        kind = request.POST.get("kind")

        if kind == "bank_charge":
            dept = Department.objects.filter(pk=request.POST.get("department")).first() \
                   or _float_fund()
            Expense.objects.create(
                date=txn.date, sabbath_week=sabbath_week_of(txn.date), department=dept,
                description=(txn.raw_narration or "Bank charge")[:200], amount=txn.amount,
                category=Expense.Category.BANK_CHARGE, method=Expense.Method.BANK,
                status=Expense.Status.PAID, paid_date=txn.date,
                recorded_by=request.user, approved_by=request.user, bank_transaction=txn)
            txn.department = dept
            txn.allocation_status = Transaction.Status.MANUAL
            txn.save(update_fields=["department", "allocation_status"])
            messages.success(request, "Recorded as a bank charge.")

        elif kind == "expense":
            dept = Department.objects.filter(pk=request.POST.get("department")).first()
            if not dept:
                messages.error(request, "Choose a fund for the expense.")
                return redirect("debit_queue")
            cat = request.POST.get("category") or Expense.Category.OTHER
            Expense.objects.create(
                date=txn.date, sabbath_week=sabbath_week_of(txn.date), department=dept,
                description=(request.POST.get("description") or txn.raw_narration or "Expense")[:200],
                amount=txn.amount, category=cat, method=Expense.Method.BANK,
                status=Expense.Status.PAID, paid_date=txn.date,
                recorded_by=request.user, approved_by=request.user, bank_transaction=txn)
            txn.department = dept
            txn.allocation_status = Transaction.Status.MANUAL
            txn.save(update_fields=["department", "allocation_status"])
            messages.success(request, "Recorded as an expense.")

        elif kind == "remittance":
            dept = Department.objects.filter(pk=request.POST.get("department")).first()
            if not dept:
                messages.error(request, "Choose the trust fund being remitted.")
                return redirect("debit_queue")
            Expense.objects.create(
                date=txn.date, sabbath_week=sabbath_week_of(txn.date), department=dept,
                description=(request.POST.get("description") or "Remittance to field")[:200],
                amount=txn.amount, category=Expense.Category.REMITTANCE,
                method=Expense.Method.BANK, status=Expense.Status.PAID,
                paid_date=txn.date, recorded_by=request.user, approved_by=request.user,
                bank_transaction=txn)
            txn.department = dept
            txn.allocation_status = Transaction.Status.MANUAL
            txn.save(update_fields=["department", "allocation_status"])
            messages.success(request, f"Recorded as a trust remittance for {dept.name}.")

        elif kind == "match":
            ids = request.POST.getlist("expense")
            exps = list(Expense.objects.filter(pk__in=ids))
            if not exps:
                messages.error(request, "Choose at least one expense to match.")
                return redirect("debit_queue")
            total = sum((e.amount for e in exps), Decimal(0))
            if abs(total - txn.amount) > Decimal("0.01"):
                messages.error(
                    request, f"The selected expense(s) total {total:,.2f} but the "
                             f"debit is {txn.amount:,.2f} — they must match.")
                return redirect("debit_queue")
            for exp in exps:
                exp.bank_transaction = txn
                if exp.status != Expense.Status.PAID:
                    exp.status = Expense.Status.PAID
                    exp.paid_date = txn.date
                exp.save()
            dept_ids = {e.department_id for e in exps}
            if len(dept_ids) == 1:
                txn.department = exps[0].department
            else:
                # one withdrawal spanning several funds: the per-fund split is held
                # by the linked expenses (each reduces its own fund); the debit line
                # itself is left unallocated and flagged as a multi-fund payment.
                txn.department = None
                names = ", ".join(sorted({e.department.name for e in exps}))
                txn.raw_narration = (txn.raw_narration or "") + \
                    f"\n[Split across {len(dept_ids)} funds: {names}]"
            txn.allocation_status = Transaction.Status.MANUAL
            txn.save()
            note = (f" across {len(dept_ids)} funds" if len(dept_ids) > 1 else "")
            messages.success(
                request, f"Matched {len(exps)} expense(s) totalling {total:,.2f}{note} to this debit.")

        elif kind == "float":
            fund = _float_fund()
            txn.department = fund
            txn.allocation_status = Transaction.Status.MANUAL
            txn.save(update_fields=["department", "allocation_status"])
            messages.success(
                request, "Marked as a float withdrawal (cash on hand). Record "
                         "expenses against it as they are paid.")

        elif kind == "petty_cash":
            # the bank withdrawal funded the petty-cash float: record a top-up so
            # the float reflects it. This moves money bank -> cash on hand; it
            # doesn't reduce total cash, so the debit is not booked as an expense.
            from cashbook.models import PettyCashTopUp
            PettyCashTopUp.objects.create(
                date=txn.date, amount=txn.amount,
                note=(request.POST.get("description") or txn.raw_narration
                      or "Bank withdrawal to petty cash")[:200],
                recorded_by=request.user)
            txn.department = _float_fund()
            txn.allocation_status = Transaction.Status.MANUAL
            txn.save(update_fields=["department", "allocation_status"])
            messages.success(
                request, f"Allocated to petty cash — the float has been topped up by "
                         f"{txn.amount:,.2f}.")
        else:
            messages.error(request, "Choose how to treat this debit.")
        return redirect("debit_queue")


# ---- Review-queue export / import (offline matching) ----
import csv as _csv
import io as _io
from django.http import HttpResponse


class QueueExportView(ReadAccessMixin, View):
    """Download the giving review queue as CSV for offline allocation."""
    def get(self, request):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="review_queue.csv"'
        w = _csv.writer(resp)
        w.writerow(["id", "date", "amount", "payer_name", "payer_phone",
                    "reference", "narration", "allocate_to_fund"])
        for t in (Transaction.objects.filter(
                allocation_status=Transaction.Status.REVIEW,
                direction=Transaction.Direction.CREDIT).order_by("date")):
            w.writerow([t.id, t.date, t.amount, t.payer_name, t.payer_phone,
                        t.reference, (t.raw_narration or "").replace("\n", " "), ""])
        return resp


class QueueImportView(DataEntryRequiredMixin, View):
    """Upload the filled queue CSV: each row's 'allocate_to_fund' (a fund name)
    allocates that transaction. Optionally remembers a rule for the reference."""
    template_name = "giving/queue_import.html"

    def get(self, request):
        return render(request, self.template_name, {})

    def post(self, request):
        f = request.FILES.get("file")
        remember = request.POST.get("remember") == "1"
        if not f:
            messages.error(request, "Choose the filled CSV.")
            return redirect("queue_import")
        try:
            reader = _csv.DictReader(_io.TextIOWrapper(f.file, encoding="utf-8-sig"))
        except Exception:
            from core.utils import log_exception as _lx; _lx('giving/views.py')
            messages.error(request, "Could not read that CSV.")
            return redirect("queue_import")
        funds = {d.name.lower(): d for d in Department.objects.filter(active=True)}
        done = skipped = 0
        for row in reader:
            fund_name = (row.get("allocate_to_fund") or "").strip().lower()
            tid = (row.get("id") or "").strip()
            if not fund_name or not tid.isdigit():
                continue
            dept = funds.get(fund_name)
            txn = Transaction.objects.filter(pk=tid,
                  allocation_status=Transaction.Status.REVIEW).first()
            if not dept or not txn:
                skipped += 1
                continue
            txn.department = dept
            txn.allocation_status = Transaction.Status.MANUAL
            txn.save(update_fields=["department", "allocation_status"])
            done += 1
            if remember and txn.reference:
                from .services.allocation import normalize_reference
                AllocationRule.objects.get_or_create(
                    reference=normalize_reference(txn.reference),
                    defaults=dict(department=dept, source=AllocationRule.Source.LEARNED))
        msg = f"Allocated {done} item(s)."
        if skipped:
            msg += f" Skipped {skipped} (unknown fund or already allocated)."
        messages.success(request, msg)
        return redirect("queue")


class MarkProcessedImportView(DataEntryRequiredMixin, View):
    """Bulk-mark bank entries as 'processed via envelope' — handled already, so
    kept out of the receipting/review flow, but NOT receipted (no envelope record
    is created). This is for contributions a member wrote on a physical envelope: the
    money is on the bank statement, but it must not be receipted again.

    Upload a small file with just a REFERENCE and an AMOUNT per row. The reference
    finds the bank transaction; the amount confirms it's the right record (a
    mismatch is reported, not applied). Accepts .csv or .xlsx.
    """
    template_name = "giving/mark_processed_import.html"

    def get(self, request):
        if request.GET.get("template"):
            return self._template()
        return render(request, self.template_name, {})

    def _template(self):
        from django.http import HttpResponse
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="mark_processed_template.csv"'
        w = _csv.writer(resp)
        w.writerow(["reference", "amount"])
        w.writerow(["UER2Q5NF2W", "1500"])
        w.writerow(["AC0C40FD2E26", "2000"])
        return resp

    def _rows_from_upload(self, f):
        """Yield (reference, amount_or_None) from a .csv or .xlsx upload, tolerant
        of header names and column order."""
        name = (getattr(f, "name", "") or "").lower()
        rows = []
        if name.endswith((".xlsx", ".xls")):
            import openpyxl
            wb = openpyxl.load_workbook(f, data_only=True)
            ws = wb.active
            data = list(ws.iter_rows(values_only=True))
            if not data:
                return rows
            header = [str(c).strip().lower() if c is not None else "" for c in data[0]]
            ref_i = next((i for i, h in enumerate(header)
                          if h in ("reference", "ref", "core ref", "receipt")), 0)
            amt_i = next((i for i, h in enumerate(header)
                          if h in ("amount", "amt", "value")), 1)
            for r in data[1:]:
                ref = str(r[ref_i]).strip() if ref_i < len(r) and r[ref_i] not in (None, "") else ""
                amt = r[amt_i] if amt_i < len(r) else None
                if ref:
                    rows.append((ref, amt))
        else:
            reader = _csv.DictReader(_io.TextIOWrapper(f.file, encoding="utf-8-sig"))
            # normalise header keys
            for raw in reader:
                row = { (k or "").strip().lower(): v for k, v in raw.items() }
                ref = (row.get("reference") or row.get("ref")
                       or row.get("core ref") or row.get("receipt") or "").strip()
                amt = row.get("amount") or row.get("amt") or row.get("value")
                if ref:
                    rows.append((ref, amt))
        return rows

    def post(self, request):
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a file with reference and amount columns.")
            return redirect("mark_processed_import")
        try:
            rows = self._rows_from_upload(f)
        except Exception:
            from core.utils import log_exception as _lx; _lx('giving/views.py')
            messages.error(request, "Could not read that file — upload the .csv or "
                                    ".xlsx from the template.")
            return redirect("mark_processed_import")
        if not rows:
            messages.warning(request, "No rows with a reference were found.")
            return redirect("mark_processed_import")

        marked = already = not_found = mismatched = ambiguous = 0
        problems = []

        def _mark(txn):
            """Mark one transaction as a manual (paper) receipt; return True if
            newly changed. Cascade is off because the importer has already matched
            the full split group by its total and marks each row explicitly."""
            return txn.mark_manual_receipt(value=True, cascade_split=False) > 0

        for ref, raw_amt in rows:
            # match a bank CREDIT by any of the reference-bearing fields
            qs = Transaction.objects.filter(
                channel=Transaction.Channel.BANK,
                direction=Transaction.Direction.CREDIT,
                is_reversal=False, is_reversed=False).filter(
                Q(reference__iexact=ref) | Q(core_ref__iexact=ref)
                | Q(bank_receipt__iexact=ref) | Q(mpesa_ref__iexact=ref))
            n = qs.count()
            if n == 0:
                not_found += 1
                if len(problems) < 12:
                    problems.append(f"“{ref}”: no matching bank entry")
                continue

            # parse the confirming amount, if supplied
            amt = None
            if raw_amt not in (None, ""):
                try:
                    amt = Decimal(str(raw_amt).replace(",", "").strip())
                except Exception:
                    from core.utils import log_exception as _lx; _lx('giving/views.py')
                    amt = None

            if n == 1:
                txn = qs.first()
                if amt is not None and txn.amount != amt:
                    mismatched += 1
                    if len(problems) < 12:
                        problems.append(f"“{ref}”: amount {amt} ≠ recorded {txn.amount}")
                    continue
                if _mark(txn):
                    marked += 1
                else:
                    already += 1
                continue

            # --- multiple matches: most often a SPLIT-FUND gift -----------------
            # A split gift (e.g. Combined Offering) is posted as several rows that
            # share the reference but divide the amount. The uploaded amount is the
            # original lump sum, so it equals the SUM of the group, not any one row.
            rows_qs = list(qs)
            total = sum((t.amount for t in rows_qs), Decimal(0))
            if amt is not None and total == amt:
                # whole split group confirmed by its total — mark every part
                newly = sum(1 for t in rows_qs if _mark(t))
                if newly:
                    marked += newly
                else:
                    already += 1
                continue
            # otherwise, an exact single-row amount match still disambiguates
            if amt is not None:
                exact = [t for t in rows_qs if t.amount == amt]
                if len(exact) == 1:
                    if _mark(exact[0]):
                        marked += 1
                    else:
                        already += 1
                    continue
            # genuinely ambiguous — report with both the count and the group total
            ambiguous += 1
            if len(problems) < 12:
                hint = (f"sum is {total}" if amt is None
                        else f"amount {amt} ≠ any row and ≠ split total {total}")
                problems.append(f"“{ref}”: matches {n} entries — {hint}")

        parts = [f"{marked} marked as manual receipt"]
        if already:
            parts.append(f"{already} already marked")
        if mismatched:
            parts.append(f"{mismatched} amount mismatch")
        if ambiguous:
            parts.append(f"{ambiguous} ambiguous")
        if not_found:
            parts.append(f"{not_found} not found")
        msg = ", ".join(parts) + "."
        if problems:
            msg += " Issues: " + "; ".join(problems)
        (messages.success if marked else messages.warning)(request, msg)
        return redirect("transaction_list")


class TransactionReverseView(TreasurerRequiredMixin, View):
    """Reverse a ledger entry (treasury never deletes — it posts a contra entry).
    Blocked inside a locked period unless an admin overrides."""
    def post(self, request, pk):
        from core.models import period_locked
        t = get_object_or_404(Transaction, pk=pk)
        lock = period_locked(t.date)
        if lock:
            messages.error(request, f"{lock} is locked. An administrator must unlock "
                                    "the period before this entry can be reversed.")
            return redirect(request.META.get("HTTP_REFERER") or "transaction_list")
        try:
            t.reverse(request.user, reason=(request.POST.get("reason") or "").strip())
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(request.META.get("HTTP_REFERER") or "transaction_list")

        # If this ledger entry came from an envelope, reverse the rest of that
        # envelope's entries too and DELETE the envelope receipt (rather than
        # leaving it struck through in the list).
        removed_envelope = self._delete_linked_envelope(t, request.user)

        if removed_envelope:
            messages.success(request, "Entry reversed and its envelope receipt removed "
                                      "— contra postings remain on the ledger for audit.")
        else:
            messages.success(request, "Entry reversed — a contra posting was created and "
                                      "both remain on the ledger for the audit trail.")
        return redirect(request.META.get("HTTP_REFERER") or "transaction_list")

    @staticmethod
    def _delete_linked_envelope(txn, user):
        """If `txn` belongs to an envelope, reverse the envelope's other ledger
        entries and delete the envelope (and its lines). Returns True if done."""
        from envelopes.models import Envelope, EnvelopeLine
        env = (Envelope.objects.filter(lines__transaction=txn).first()
               or Envelope.objects.filter(bank_transaction=txn).first())
        if env is None:
            return False
        for st in env.linked_transactions:
            if st and st.pk != txn.pk and not st.is_reversed and not st.is_reversal:
                try:
                    st.reverse(user, reason=f"Envelope #{env.receipt_no} reversed")
                except ValueError:
                    pass
        env.delete()   # cascades EnvelopeLine rows; reversed ledger entries remain
        return True


class TransactionSplitView(TreasurerRequiredMixin, View):
    """Split one lump-sum entry across several funds and/or development groups
    (e.g. a single 2,000 bank deposit meant for two groups)."""
    template_name = "giving/transaction_split.html"

    def get(self, request, pk):
        from departments.models import Department, DevelopmentGroup
        t = get_object_or_404(Transaction, pk=pk)
        return render(request, self.template_name, {
            "txn": t,
            "departments": Department.objects.filter(active=True, selectable=True).order_by("name"),
            "dev_groups": DevelopmentGroup.objects.filter(active=True).order_by("number"),
        })

    def post(self, request, pk):
        from departments.models import Department, DevelopmentGroup
        from core.models import period_locked
        t = get_object_or_404(Transaction, pk=pk)
        lock = period_locked(t.date)
        if lock:
            messages.error(request, f"{lock} is locked. Unlock the period first.")
            return redirect("transaction_list")
        depts = request.POST.getlist("department")
        amounts = request.POST.getlist("amount")
        groups = request.POST.getlist("dev_group")
        parts = []
        for i, did in enumerate(depts):
            amt = amounts[i] if i < len(amounts) else None
            if not did or amt in (None, ""):
                continue
            dept = Department.objects.filter(pk=did).first()
            gid = groups[i] if i < len(groups) else ""
            grp = DevelopmentGroup.objects.filter(pk=gid).first() if gid else None
            parts.append((dept, amt, grp))
        try:
            out = t.split_into(parts, request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("transaction_split", pk=pk)
        messages.success(request, f"Split into {len(out)} allocations.")
        return redirect("transaction_list")


class TransactionShiftSabbathView(TreasurerRequiredMixin, View):
    """Move a contribution to the next or previous Sabbath WITHOUT changing its real
    transaction date — used for late/after-cutoff items so a closed Sabbath is
    never altered. Audit-tracked via history."""
    def post(self, request, pk):
        from django.shortcuts import get_object_or_404
        from django.urls import reverse
        import datetime as _dt
        from core.utils import sabbath_of, sabbath_week_of
        t = get_object_or_404(Transaction, pk=pk)
        direction = request.POST.get("dir", "next")
        current = t.service_sabbath or sabbath_of(t.date)
        new = current + _dt.timedelta(days=7 if direction == "next" else -7)
        # don't allow shifting into a locked period
        try:
            from core.models import period_locked
            if period_locked(new) or period_locked(current):
                messages.error(request, "That Sabbath falls in a locked period — "
                                        "unlock it first.")
                return redirect(request.META.get("HTTP_REFERER", reverse("transaction_list")))
        except Exception:
            from core.utils import log_exception as _lx; _lx('giving/views.py')
            pass
        t.service_sabbath = new
        t.sabbath_week = sabbath_week_of(new)
        t.save(update_fields=["service_sabbath", "sabbath_week"])
        messages.success(request, f"Contribution moved to the Sabbath of {new:%d %b %Y} "
                                  f"(transaction date unchanged: {t.date:%d %b %Y}).")
        return redirect(request.META.get("HTTP_REFERER", reverse("transaction_list")))


class SabbathConfirmQueueView(DataEntryRequiredMixin, View):
    """Contributions imported after their service Sabbath had already passed — confirm
    whether each stays on that Sabbath or moves to the next one. Grouped per
    Sabbath so a whole import can be confirmed in one click."""
    template_name = "giving/sabbath_queue.html"

    def _qs(self):
        return (Transaction.objects.filter(sabbath_confirm_pending=True)
                .select_related("department", "member")
                .order_by("service_sabbath", "-date", "-id"))

    def get(self, request):
        from itertools import groupby
        from django.db.models import Sum as _S
        qs = list(self._qs())
        groups = []
        for sab, items in groupby(qs, key=lambda t: t.service_sabbath):
            items = list(items)
            groups.append({"sabbath": sab, "items": items,
                           "count": len(items),
                           "total": sum((t.amount for t in items), Decimal(0))})
        return render(request, self.template_name, {"groups": groups,
                                                    "pending_total": len(qs)})

    def post(self, request):
        import datetime as _dt
        from core.models import next_open_sabbath, entry_blocked
        from core.utils import sabbath_week_of as _swk
        action = request.POST.get("action")
        ids = request.POST.getlist("txn")
        sab_raw = request.POST.get("sabbath")
        qs = Transaction.objects.filter(sabbath_confirm_pending=True)
        if sab_raw and not ids:        # whole-group action
            try:
                qs = qs.filter(service_sabbath=_dt.date.fromisoformat(sab_raw))
            except ValueError:
                qs = qs.none()
        elif ids:
            qs = qs.filter(id__in=ids)
        else:
            qs = qs.none()
        n = 0
        if action == "keep":
            n = qs.update(sabbath_confirm_pending=False)
            messages.success(request, f"Kept {n} contribution(s) on their original Sabbath.")
        elif action == "move":
            for t in qs:
                target = next_open_sabbath(t.service_sabbath + _dt.timedelta(days=7))
                why = entry_blocked(target)
                if why:
                    messages.error(request, f"Could not move a contribution: {why}")
                    continue
                t.service_sabbath = target
                t.sabbath_week = _swk(target)
                t.sabbath_confirm_pending = False
                t.save(update_fields=["service_sabbath", "sabbath_week",
                                      "sabbath_confirm_pending"])
                n += 1
            messages.success(request, f"Moved {n} contribution(s) to the next Sabbath.")
        return redirect("sabbath_queue")


# ===========================================================================
# Allocation-rules Excel import (item 1)
# ===========================================================================
class RuleImportView(TreasurerRequiredMixin, View):
    """Bulk-load allocation rules from a spreadsheet: reference, match type, the
    fund (or split fund) to allocate to, and optional valid-from/to dates."""
    template_name = "giving/rule_import.html"

    MATCH_LABELS = {
        "EXACT": "EXACT", "EXACTLY": "EXACT", "MATCHES EXACTLY": "EXACT", "IS": "EXACT",
        "STARTS": "STARTS", "STARTS WITH": "STARTS", "BEGINS": "STARTS", "PREFIX": "STARTS",
        "ENDS": "ENDS", "ENDS WITH": "ENDS", "SUFFIX": "ENDS",
        "CONTAINS": "CONTAINS", "INCLUDES": "CONTAINS", "HAS": "CONTAINS",
        "REGEX": "REGEX", "PATTERN": "REGEX", "MATCHES A PATTERN (REGEX)": "REGEX",
    }

    def get(self, request):
        if request.GET.get("download"):
            return self._download()
        return render(request, self.template_name, {"stage": "upload"})

    def post(self, request):
        if request.POST.get("apply"):
            return self._apply(request)
        return self._parse(request)

    def _download(self):
        import io, openpyxl
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
        from django.http import HttpResponse
        from departments.models import Department
        from giving.models import SplitFund
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Rules"
        head = ["Reference", "Match type", "Fund", "Split fund", "Valid from", "Valid to"]
        ws.append(head)
        for c in range(1, len(head) + 1):
            ws.cell(1, c).font = Font(bold=True, color="FFFFFF")
            ws.cell(1, c).fill = PatternFill("solid", fgColor="1F5F4F")
        ws.append(["tithe", "Exact", "TITHE", "", "", ""])
        ws.append(["grp12dev", "Exact", "DEVELOPMENT", "", "", ""])
        ws.append(["expense1", "Starts with", "", "Combined Offering", "", ""])
        ref = wb.create_sheet("Lists")
        ref["A1"] = "Funds"; ref["A1"].font = Font(bold=True)
        funds = list(Department.objects.filter(active=True, selectable=True).order_by("name"))
        for i, d in enumerate(funds, start=2):
            ref.cell(i, 1, d.name)
        ref["B1"] = "Match types"; ref["B1"].font = Font(bold=True)
        for i, m in enumerate(["Exact", "Starts with", "Ends with", "Contains"], start=2):
            ref.cell(i, 2, m)
        ref["C1"] = "Split funds"; ref["C1"].font = Font(bold=True)
        splits = list(SplitFund.objects.filter(active=True).order_by("name"))
        for i, s in enumerate(splits, start=2):
            ref.cell(i, 3, s.name)
        nrows = 400
        if funds:
            dv = DataValidation(type="list", formula1=f"=Lists!$A$2:$A${len(funds)+1}", allow_blank=True)
            ws.add_data_validation(dv); dv.add(f"C2:C{nrows}")
        dvm = DataValidation(type="list", formula1="=Lists!$B$2:$B$5", allow_blank=True)
        ws.add_data_validation(dvm); dvm.add(f"B2:B{nrows}")
        if splits:
            dvs = DataValidation(type="list", formula1=f"=Lists!$C$2:$C${len(splits)+1}", allow_blank=True)
            ws.add_data_validation(dvs); dvs.add(f"D2:D{nrows}")
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["C"].width = 22
        ws.column_dimensions["D"].width = 20
        info = wb.create_sheet("How to fill this in")
        for i, line in enumerate([
            "Allocation rules import",
            "",
            "One row per rule. A rule sends a payment reference to a fund.",
            "  - Reference — the M-Pesa/bank reference text (e.g. tithe, grp12dev).",
            "      It is matched case- and space-insensitively.",
            "  - Match type — Exact / Starts with / Ends with / Contains. Use",
            "      'Contains' to catch variations (e.g. exp1, expense1 all contain 'exp1'?",
            "      pick the common fragment).",
            "  - Fund — the fund to allocate to (pick from the list). Leave blank if",
            "      you are using a Split fund instead.",
            "  - Split fund — if the reference should split across funds (e.g. a",
            "      combined offering), name it here and leave Fund blank.",
            "  - Valid from / Valid to — optional (YYYY-MM-DD). Leave both blank for a",
            "      permanent rule.",
            "",
            "Give either a Fund or a Split fund on each row, not both.",
            "Existing rules with the same reference are updated.",
        ], start=1):
            info.cell(i, 1, line)
        info.column_dimensions["A"].width = 76
        buf = io.BytesIO(); wb.save(buf)
        resp = HttpResponse(buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = 'attachment; filename="allocation_rules_template.xlsx"'
        return resp

    def _parse(self, request):
        import openpyxl, datetime as dt
        from departments.models import Department
        from giving.models import SplitFund
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a spreadsheet to upload.")
            return redirect("rule_import")
        try:
            wb = openpyxl.load_workbook(f, data_only=True)
        except Exception:
            from core.utils import log_exception as _lx; _lx('giving/views.py')
            messages.error(request, "Could not read that file — please upload a .xlsx.")
            return redirect("rule_import")
        ws = wb["Rules"] if "Rules" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            messages.error(request, "The sheet is empty.")
            return redirect("rule_import")
        header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]

        def col(*names):
            for n in names:
                if n in header:
                    return header.index(n)
            return None
        c_ref = col("reference", "ref")
        c_match = col("match type", "match", "type")
        c_fund = col("fund", "department")
        c_split = col("split fund", "split")
        c_from = col("valid from", "from")
        c_to = col("valid to", "to")
        if c_ref is None:
            messages.error(request, "Couldn't find a Reference column — use the template.")
            return redirect("rule_import")

        funds = {d.name.strip().lower(): d for d in Department.objects.all()}
        splits = {s.name.strip().lower(): s for s in SplitFund.objects.all()}

        def cell(r, idx):
            if idx is None or idx >= len(r) or r[idx] in (None, ""):
                return ""
            return str(r[idx]).strip()

        def pdate(r, idx):
            if idx is None or idx >= len(r) or r[idx] in (None, ""):
                return None
            v = r[idx]
            if isinstance(v, dt.datetime):
                return v.date().isoformat()
            if isinstance(v, dt.date):
                return v.isoformat()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
                try:
                    return dt.datetime.strptime(str(v).strip(), fmt).date().isoformat()
                except ValueError:
                    continue
            return None

        plan = []
        for r in rows[1:]:
            ref = normalize_reference(cell(r, c_ref))
            if not ref:
                continue
            match = self.MATCH_LABELS.get(cell(r, c_match).upper(), "EXACT")
            fund_raw = cell(r, c_fund)
            split_raw = cell(r, c_split)
            fund = funds.get(fund_raw.lower()) if fund_raw else None
            split = splits.get(split_raw.lower()) if split_raw else None
            plan.append({
                "reference": ref, "match_type": match,
                "fund_raw": fund_raw, "fund_id": fund.id if fund else None,
                "fund_name": fund.name if fund else None,
                "split_raw": split_raw, "split_id": split.id if split else None,
                "split_name": split.name if split else None,
                "valid_from": pdate(r, c_from), "valid_to": pdate(r, c_to),
                "ok": bool(fund or split) and not (fund and split),
            })
        if not plan:
            messages.error(request, "No rules with a reference were found.")
            return redirect("rule_import")
        request.session["rule_import_plan"] = plan
        return render(request, self.template_name, {
            "stage": "review", "plan": plan,
            "ready": sum(1 for p in plan if p["ok"]),
            "problems": sum(1 for p in plan if not p["ok"]),
        })

    @db_tx.atomic
    def _apply(self, request):
        from departments.models import Department
        from giving.models import SplitFund
        plan = request.session.get("rule_import_plan")
        if not plan:
            messages.error(request, "Your import session expired — please upload again.")
            return redirect("rule_import")
        created = updated = skipped = 0
        for p in plan:
            if not p["ok"]:
                skipped += 1
                continue
            fund = Department.objects.filter(pk=p["fund_id"]).first() if p["fund_id"] else None
            split = SplitFund.objects.filter(pk=p["split_id"]).first() if p["split_id"] else None
            if not fund and not split:
                skipped += 1
                continue
            obj, was_created = AllocationRule.objects.update_or_create(
                reference=p["reference"],
                defaults={"match_type": p["match_type"], "department": fund,
                          "split_fund": split, "source": AllocationRule.Source.SEED,
                          "valid_from": p["valid_from"], "valid_to": p["valid_to"]})
            created += 1 if was_created else 0
            updated += 0 if was_created else 1
        request.session.pop("rule_import_plan", None)
        parts = [f"{created} rule(s) created"]
        if updated:
            parts.append(f"{updated} updated")
        if skipped:
            parts.append(f"{skipped} skipped (no fund, or both fund and split set)")
        messages.success(request, ", ".join(parts) + ".")
        return redirect("rule_list")


class CashEntryDeleteView(DataEntryRequiredMixin, View):
    """Delete a loose cash entry. A cash entry IS its ledger row (one Transaction),
    so this removes the single record — there's no separate copy to fall out of
    sync. Split cash entries delete all their parts together. Edits still happen
    at the ledger. Guarded: only manual CASH credits that aren't reconciled,
    reversed, receipted via an envelope, or in a locked period."""

    def post(self, request, pk):
        txn = get_object_or_404(Transaction, pk=pk)
        # only loose cash credits may be deleted here
        if txn.channel != Transaction.Channel.CASH or \
           txn.direction != Transaction.Direction.CREDIT:
            messages.error(request, "Only cash collections can be deleted here. "
                           "Use the ledger for other entries.")
            return redirect("cash_list")
        if _block_if_locked(request, txn.date):
            return redirect("cash_list")
        # don't delete something tied to other records or already reversed
        blockers = []
        if txn.is_reversed or txn.is_reversal:
            blockers.append("it has already been reversed")
        if getattr(txn, "processed_via_envelope", False):
            blockers.append("it was receipted via an envelope")
        if txn.envelope_lines.exists():
            blockers.append("it is linked to an envelope")
        if blockers:
            messages.error(request, "Can't delete this cash entry because "
                           + " and ".join(blockers) + ". Reverse it at the ledger instead.")
            return redirect("cash_list")
        siblings = list(txn.split_siblings()) + [txn]
        n = len(siblings)
        with db_tx.atomic():
            for s in siblings:
                s.delete()
        messages.success(request,
            f"Cash entry deleted{f' ({n} split parts)' if n > 1 else ''}. "
            "The ledger row was removed with it.")
        return redirect("cash_list")


# --- Campaign fallback allocation -------------------------------------------
class CampaignListView(ReadAccessMixin, View):
    """Manage campaigns (e.g. Camp Meeting): their fund, trigger words and the
    member→group table used as a fallback when the normal rules miss."""
    template_name = "giving/campaign_list.html"

    def get(self, request):
        from giving.models import Campaign
        from departments.models import Department
        from django.db.models import Count
        camps = (Campaign.objects.select_related("department")
                 .annotate(n_members=Count("members"), n_txns=Count("transactions"))
                 .order_by("-active", "name"))
        return render(request, self.template_name, {
            "campaigns": camps,
            "funds": Department.objects.filter(active=True, selectable=True).order_by("name"),
        })


class CampaignCreateView(DataEntryRequiredMixin, View):
    def post(self, request):
        from giving.models import Campaign
        from departments.models import Department
        name = (request.POST.get("name") or "").strip()
        dept = Department.objects.filter(pk=request.POST.get("department")).first()
        triggers = (request.POST.get("triggers") or "").strip()
        if not name or not dept:
            messages.error(request, "A campaign needs a name and a fund.")
            return redirect("campaign_list")
        camp, created = Campaign.objects.get_or_create(
            name=name, defaults={"department": dept, "triggers": triggers})
        if not created:
            camp.department = dept
            camp.triggers = triggers
            camp.active = True
            camp.save()
        messages.success(request, f"Campaign “{name}” saved. Now upload its members.")
        return redirect("campaign_list")


class CampaignMemberImportView(DataEntryRequiredMixin, View):
    """Upload the Name / Mobile / Group sheet for a campaign. Reads .xlsx or .csv
    tolerantly, skips unusable rows and reports what happened — a bad row never
    aborts the whole upload."""
    @staticmethod
    def _phone_cell(v):
        # a numeric phone cell arrives as int/float (e.g. 254791896792.0)
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()

    def _read(self, f):
        name = (getattr(f, "name", "") or "").lower()
        rows = []
        if name.endswith((".xlsx", ".xls")):
            import openpyxl
            ws = openpyxl.load_workbook(f, data_only=True).active
            data = list(ws.iter_rows(values_only=True))
            if not data:
                return rows
            hdr = [str(c).strip().lower() if c else "" for c in data[0]]
            ni = next((i for i, h in enumerate(hdr) if "name" in h), 0)
            pi = next((i for i, h in enumerate(hdr) if h in ("mobile", "phone", "msisdn")), 1)
            gi = next((i for i, h in enumerate(hdr) if "group" in h), 2)
            for r in data[1:]:
                nm = str(r[ni]).strip() if ni < len(r) and r[ni] not in (None, "") else ""
                rows.append((nm, self._phone_cell(r[pi] if pi < len(r) else None),
                             str(r[gi]).strip() if gi < len(r) and r[gi] else ""))
        else:
            import csv as _csv, io as _io
            for raw in _csv.DictReader(_io.TextIOWrapper(f.file, encoding="utf-8-sig")):
                row = {(k or "").strip().lower(): v for k, v in raw.items()}
                rows.append(((row.get("name") or "").strip(),
                             (row.get("mobile") or row.get("phone") or "").strip(),
                             (row.get("group") or "").strip()))
        return rows

    def post(self, request, pk):
        from giving.models import Campaign, CampaignMember
        camp = get_object_or_404(Campaign, pk=pk)
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a .xlsx or .csv with Name, Mobile, Group columns.")
            return redirect("campaign_list")
        try:
            rows = self._read(f)
        except Exception:
            from core.utils import log_exception as _lx; _lx('giving/views.py')
            messages.error(request, "Could not read that file — use the sample layout "
                                    "(Name, Mobile, Group). Try downloading the sample.")
            return redirect("campaign_list")
        if not rows:
            messages.warning(request, "That file had no data rows.")
            return redirect("campaign_list")
        camp.members.all().delete()          # replace the table
        made = skipped = 0
        for nm, ph, grp in rows:
            if not nm:
                skipped += 1
                continue
            try:
                CampaignMember.objects.create(campaign=camp, name=nm, phone=ph, group=grp)
                made += 1
            except Exception:
                from core.utils import log_exception as _lx; _lx('giving/views.py')
                skipped += 1
        msg = f"Loaded {made} members into “{camp.name}”."
        if skipped:
            msg += f" {skipped} row(s) skipped (no name or unreadable)."
        (messages.success if made else messages.warning)(request, msg)
        return redirect("campaign_list")


class CampaignTemplateView(ReadAccessMixin, View):
    """Download a sample member-upload file (CSV)."""
    def get(self, request):
        import csv as _csv
        from django.http import HttpResponse
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="campaign_members_sample.csv"'
        w = _csv.writer(resp)
        w.writerow(["Name", "Mobile", "Group"])
        w.writerow(["Amos Ndegwa", "254791896792", "CAMP_1"])
        w.writerow(["Calvince Ouma", "0726410608", "CAMP_1"])
        w.writerow(["Caroline Nyalick", "254705321239", "CAMP_2"])
        return resp


class CampaignDeleteView(TreasurerRequiredMixin, View):
    """Delete a finished campaign: its member table goes; rows it allocated keep
    their group tag (campaign link is set null)."""
    def post(self, request, pk):
        from giving.models import Campaign
        camp = get_object_or_404(Campaign, pk=pk)
        nm = camp.name
        camp.delete()
        messages.success(request, f"Campaign “{nm}” deleted. Past allocations keep their group tag.")
        return redirect("campaign_list")


class TransactionBulkReverseView(TreasurerRequiredMixin, View):
    """Reverse several selected ledger entries at once. Treasury never hard-
    deletes — each becomes a contra posting (and a linked envelope receipt is
    removed, its siblings reversed). Locked-period rows and ones already reversed
    (or that are themselves reversals) are skipped and counted."""
    def post(self, request):
        from core.models import period_locked
        ids = request.POST.getlist("ids")
        if not ids:
            messages.info(request, "No entries were selected.")
            return redirect(request.META.get("HTTP_REFERER") or "transaction_list")
        reason = (request.POST.get("reason") or "").strip()
        done = skipped = 0
        for t in Transaction.objects.filter(pk__in=ids):
            if t.is_reversed or t.is_reversal or period_locked(t.date):
                skipped += 1
                continue
            try:
                t.reverse(request.user, reason=reason)
            except ValueError:
                skipped += 1
                continue
            TransactionReverseView._delete_linked_envelope(t, request.user)
            done += 1
        msg = f"{done} entr{'y' if done == 1 else 'ies'} reversed (contra postings kept for audit)."
        if skipped:
            msg += f" {skipped} skipped (locked period, or already reversed)."
        (messages.success if done else messages.info)(request, msg)
        return redirect(request.META.get("HTTP_REFERER") or "transaction_list")


class RuleEditView(DataEntryRequiredMixin, View):
    """Edit an allocation rule (reference / match type / target fund)."""
    template_name = "giving/rule_form.html"

    def get(self, request, pk):
        rule = get_object_or_404(AllocationRule, pk=pk)
        return render(request, self.template_name,
                      {"form": RuleForm(instance=rule), "edit_obj": rule})

    def post(self, request, pk):
        rule = get_object_or_404(AllocationRule, pk=pk)
        form = RuleForm(request.POST, instance=rule)
        if form.is_valid():
            form.instance.reference = normalize_reference(form.instance.reference)
            form.save()
            messages.success(request, "Rule updated.")
            return redirect("rule_list")
        return render(request, self.template_name, {"form": form, "edit_obj": rule})
