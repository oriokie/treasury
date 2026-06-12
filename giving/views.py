from decimal import Decimal

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


def _block_if_locked(request, d):
    """Return True (and flash an error) if date d is in a locked period. No one —
    including superusers — may post into a locked period; it must be unlocked first."""
    from core.models import period_locked
    lock = period_locked(d)
    if lock:
        from django.contrib import messages as _m
        _m.error(request, f"{lock} is locked. Unlock the period (Controls) before posting or editing entries in it.")
        return True
    return False
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
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
                      "Core ref", "Bank receipt", "Status", "Confirmed", "Amount"]
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
                     t.bank_receipt or "", t.get_allocation_status_display(),
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
            qs = qs.filter(Q(payer_name__icontains=q) | Q(reference__icontains=q) |
                           Q(core_ref__icontains=q) | Q(raw_narration__icontains=q))
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
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
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
        # REVIEW status are handled in the separate bank-debit queue.
        return (Transaction.objects.filter(
                    allocation_status=Transaction.Status.REVIEW,
                    direction=Transaction.Direction.CREDIT)
                .select_related("member").order_by("date", "id"))

    def get_context_data(self, **kwargs):
        from departments.models import DevelopmentGroup
        from giving.models import SplitFund
        ctx = super().get_context_data(**kwargs)
        ctx["departments"] = Department.objects.filter(active=True, selectable=True)
        ctx["split_funds"] = SplitFund.objects.filter(active=True).order_by("name")
        ctx["dev_groups"] = DevelopmentGroup.objects.filter(active=True).order_by("number")
        return ctx


class ClaimResolveView(DataEntryRequiredMixin, View):
    """Claim + resolve a review item; optionally remember the rule."""

    def post(self, request, pk):
        txn = get_object_or_404(Transaction, pk=pk,
                                allocation_status=Transaction.Status.REVIEW,
                                direction=Transaction.Direction.CREDIT)
        # --- split allocation: one bank gift meant for several funds ---
        if request.POST.get("split") == "1":
            from departments.models import Department as _D, DevelopmentGroup as _G
            parts = []
            grps = request.POST.getlist("split_grp")
            for n, (d_id, amt) in enumerate(zip(request.POST.getlist("split_dept"),
                                                request.POST.getlist("split_amount"))):
                if not d_id or not str(amt).strip():
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
                defaults={"department": dept, "source": AllocationRule.Source.LEARNED},
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
            for dept, amt in split.split(base.amount):
                Transaction.objects.create(
                    date=base.date, channel=base.channel,
                    direction=Transaction.Direction.CREDIT,
                    allocation_status=Transaction.Status.MANUAL,
                    sabbath_week=sabbath_week_of(base.date),
                    amount=amt, department=dept, member=base.member,
                    reference=base.reference, payer_name=base.payer_name)
            messages.success(self.request, f"Entry recorded and split across {split.name}.")
            return redirect(self.success_url)
        txn = form.save(commit=False)
        txn.direction = Transaction.Direction.CREDIT
        txn.allocation_status = Transaction.Status.MANUAL
        txn.sabbath_week = sabbath_week_of(txn.date)
        txn.save()
        messages.success(self.request, "Entry recorded.")
        return redirect(self.success_url)


class RuleListView(DataEntryRequiredMixin, ListView):
    model = AllocationRule
    template_name = "giving/rule_list.html"
    context_object_name = "rules"

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
        messages.success(self.request, "Entry updated.")
        return super().form_valid(form)


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
    """Move a gift to the next or previous Sabbath WITHOUT changing its real
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
            pass
        t.service_sabbath = new
        t.sabbath_week = sabbath_week_of(new)
        t.save(update_fields=["service_sabbath", "sabbath_week"])
        messages.success(request, f"Gift moved to the Sabbath of {new:%d %b %Y} "
                                  f"(transaction date unchanged: {t.date:%d %b %Y}).")
        return redirect(request.META.get("HTTP_REFERER", reverse("transaction_list")))


class SabbathConfirmQueueView(DataEntryRequiredMixin, View):
    """Gifts imported after their service Sabbath had already passed — confirm
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
            messages.success(request, f"Kept {n} gift(s) on their original Sabbath.")
        elif action == "move":
            for t in qs:
                target = next_open_sabbath(t.service_sabbath + _dt.timedelta(days=7))
                why = entry_blocked(target)
                if why:
                    messages.error(request, f"Could not move a gift: {why}")
                    continue
                t.service_sabbath = target
                t.sabbath_week = _swk(target)
                t.sabbath_confirm_pending = False
                t.save(update_fields=["service_sabbath", "sabbath_week",
                                      "sabbath_confirm_pending"])
                n += 1
            messages.success(request, f"Moved {n} gift(s) to the next Sabbath.")
        return redirect("sabbath_queue")
