from decimal import Decimal
from django.db import models
from django.db.models import Sum
from django.core.validators import MinValueValidator
from simple_history.models import HistoricalRecords


class Expense(models.Model):
    class Category(models.TextChoices):
        ALLOWANCE = "ALLOWANCE", "Allowance / honoraria"
        TRANSPORT = "TRANSPORT", "Transport"
        REFRESHMENTS = "REFRESHMENTS", "Refreshments / catering"
        MATERIALS = "MATERIALS", "Materials / supplies"
        STATIONERY = "STATIONERY", "Stationery / printing"
        SALARIES = "SALARIES", "Salaries / wages"
        LEASE = "LEASE", "Lease payment"
        UTILITIES = "UTILITIES", "Utilities (power, water)"
        MAINTENANCE = "MAINTENANCE", "Maintenance / repairs"
        CONSTRUCTION = "CONSTRUCTION", "Construction / development"
        EVANGELISM = "EVANGELISM", "Evangelism / mission"
        BENEVOLENCE = "BENEVOLENCE", "Benevolence / welfare"
        BANK_CHARGE = "BANK_CHARGE", "Bank charges"
        REMITTANCE = "REMITTANCE", "Remittance to field"
        LOAN_REPAYMENT = "LOAN_REPAYMENT", "Loan principal repayment"
        LOAN_INTEREST = "LOAN_INTEREST", "Loan interest"
        OTHER = "OTHER", "Other"

    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK = "BANK", "Bank"
        CHEQUE = "CHEQUE", "Cheque"
        MPESA = "MPESA", "M-Pesa"

    class ExpenditureType(models.TextChoices):
        RECURRENT = "RECURRENT", "Recurrent (running cost)"
        CAPITAL = "CAPITAL", "Capital (asset / development)"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        PAID = "PAID", "Paid"
        REJECTED = "REJECTED", "Rejected"

    class FundingSource(models.TextChoices):
        CONTRIBUTION = "CONTRIBUTION", "Contribution"
        LOAN = "LOAN", "Loan"
        GRANT = "GRANT", "Grant"
        ADVANCE = "ADVANCE", "Advance"
        TRANSFER = "TRANSFER", "Transfer"
        REFUND = "REFUND", "Refund"
        OTHER = "OTHER", "Other"

    class DocClass(models.TextChoices):
        """High-level transaction class — determines WHERE a document appears
        (Expense Register vs Liability Register vs future registers), never
        HOW it posts. The posting engine keys off `category` exactly as
        before; this classification is presentation and reporting only.

        RECEIPT / TRANSFER / JOURNAL / ADJUSTMENT are reserved for future
        document types; today's vouchers are either an operational EXPENSE or
        a LIABILITY movement (a balance-sheet settlement, not expenditure)."""
        RECEIPT = "RECEIPT", "Receipt"
        EXPENSE = "EXPENSE", "Expense"
        LIABILITY = "LIABILITY", "Liability"
        TRANSFER = "TRANSFER", "Transfer"
        JOURNAL = "JOURNAL", "Journal"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"

    date = models.DateField(db_index=True)
    sabbath_week = models.PositiveSmallIntegerField(null=True, blank=True)
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="expenses", db_index=True)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Always positive; a refund/reversal is its own record (ExpenseRefund), "
                  "never a negative expense — a negative amount here would post as an "
                  "unreviewed credit to cash while still being categorised as an expense, "
                  "bypassing normal income recognition entirely.")
    category = models.CharField(max_length=14, choices=Category.choices,
                                default=Category.OTHER)
    doc_class = models.CharField(max_length=10, choices=DocClass.choices,
        default=DocClass.EXPENSE, db_index=True, editable=False,
        help_text="Derived from the category on save: liability categories "
                  "(trust remittance, loan repayment, custom categories flagged "
                  "as liability) file under the Liability Register, everything "
                  "else under Expenses. Never affects ledger posting.")
    funding_source = models.CharField(max_length=12, choices=FundingSource.choices,
        default=FundingSource.CONTRIBUTION, db_index=True,
        help_text="What kind of money paid for this — contributions (the default), "
                  "loan financing, a grant, etc. Informational tagging usable "
                  "anywhere a payment is recorded.")
    expenditure_type = models.CharField(
        max_length=10, choices=ExpenditureType.choices,
        default=ExpenditureType.RECURRENT, db_index=True,
        help_text="Recurrent = day-to-day running cost; Capital = creates or "
                  "improves a fixed asset (construction, equipment, vehicles).")
    recurring = models.ForeignKey(
        "cashbook.RecurringExpense", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="generated", help_text="If set: the recurring schedule that created this expense.")
    recurring_due_date = models.DateField(
        null=True, blank=True, db_index=True,
        help_text="Which scheduled instalment this row settles. Normally the "
                  "same as the date; different when the payment was made early.")
    payable = models.ForeignKey(
        "cashbook.Payable", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="payments",
        help_text="The payable this payment settles, in whole or in part. A "
                  "payable may be paid over several instalments, so this is a "
                  "ForeignKey and not a OneToOne — see Payable.paid_total.")
    vendor = models.ForeignKey(
        "vendors.Vendor", null=True, blank=True, on_delete=models.PROTECT,
        related_name="expenses",
        help_text="The supplier this was paid to, where they are on the "
                  "supplier register. Optional: `payee` still records what the "
                  "voucher said, and a one-off payment needs no register entry.")
    accrual = models.ForeignKey(
        "cashbook.Accrual", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="payments",
        help_text="The accrual this payment settles, in whole or in part.")
    advance = models.ForeignKey(
        "cashbook.StaffAdvance", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="expenses", help_text="If this expense settles a staff cash advance, link it here.")
    charge_for = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE,
        related_name="charges",
        help_text="If this is an M-Pesa/bank transaction charge, the expense that incurred it.")
    capitalized_asset = models.ForeignKey(
        "assets.FixedAsset", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="source_expenses",
        help_text="If capital: the fixed asset this expenditure created or improved.")
    claimant = models.CharField(
        max_length=120, blank=True,
        help_text="Who incurred or requested this — the person the church is "
                  "answerable to for the claim.")
    payee = models.CharField(
        max_length=160, blank=True,
        help_text="Who the money is actually PAID TO, if that is not the claimant — "
                  "a supplier's name on a cheque, say, where the claimant is the "
                  "member who requested the purchase. Left blank, the claimant is "
                  "the payee.")
    method = models.CharField(max_length=8, choices=Method.choices, default=Method.CASH)
    voucher_no = models.CharField(max_length=30, blank=True)
    paid_from_petty_cash = models.BooleanField(default=False,
        help_text="Paid out of the petty cash float (reduces the float, charged to its fund).")
    budget_line = models.ForeignKey("cashbook.BudgetLine", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="expenses",
        help_text="The specific budget item this expense relates to (optional).")

    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="expenses_recorded")
    approved_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="expenses_approved")
    second_approved_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="expenses_second_approved",
                                    help_text="Second treasurer who co-approved a high-value expense.")
    rejected_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="expenses_rejected",
                                    help_text="Treasurer who rejected the claim (kept distinct from approver).")
    paid_date = models.DateField(null=True, blank=True)

    bank_transaction = models.ForeignKey("giving.Transaction", null=True, blank=True,
                                          on_delete=models.SET_NULL,
                                          related_name="matched_expenses")
    remittance_batch = models.ForeignKey("cashbook.RemittanceBatch", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="expenses")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [models.Index(fields=["date", "department"]),
                   models.Index(fields=["status"]),
                   # the dominant shape across reports: effective expenses
                   # (APPROVED/PAID) within a period — status alone doesn't
                   # help once a date range is added on top
                   models.Index(fields=["status", "date"])]

    def save(self, *args, **kwargs):
        # doc_class follows the category (single source of truth): liability
        # categories file under the Liability Register, the rest are expenses.
        # Recomputed on every save so a category edit refiles the voucher.
        self.doc_class = classify_category(self.category)
        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            kwargs["update_fields"] = list(
                set(kwargs["update_fields"]) | {"doc_class"})
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.date} {self.description} {self.amount}"

    @property
    def category_display(self):
        return category_label(self.category)

    @property
    def affects_balance(self):
        return self.status in (self.Status.APPROVED, self.Status.PAID)

    @property
    def refunds_total(self):
        return self.refunds.aggregate(t=Sum("amount"))["t"] or Decimal(0)

    @property
    def net_amount(self):
        """The expense actually borne by the fund after any refunds returned."""
        return (self.amount or Decimal(0)) - self.refunds_total

    @property
    def refundable_balance(self):
        """How much of this expense could still be refunded."""
        if not self.affects_balance:
            return Decimal(0)
        return max(Decimal(0), (self.amount or Decimal(0)) - self.refunds_total)


class ExpenseRefund(models.Model):
    """Money returned to a fund against an expense — e.g. unspent cash from an
    over-issued purchase (KSh 5,000 issued, KSh 4,200 spent, KSh 800 returned).

    It is a *contra-entry*: it never alters the original expense (the
    authorization for the full amount stands on record), it reduces the NET
    expense charged to the fund, and it restores that fund's available balance
    by the returned amount."""
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE,
                                related_name="refunds")
    date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    method = models.CharField(max_length=8, choices=Expense.Method.choices,
                              default=Expense.Method.CASH)
    to_petty_cash = models.BooleanField(default=False,
        help_text="Returned into the petty-cash float (tops the float back up).")
    reference = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="refunds_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Refund {self.amount} on {self.expense_id}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.amount is None or self.amount <= 0:
            raise ValidationError("Refund amount must be greater than zero.")
        if self.expense_id:
            if not self.expense.affects_balance:
                raise ValidationError("Only approved or paid expenses can be refunded.")
            already = self.expense.refunds.exclude(pk=self.pk).aggregate(
                t=Sum("amount"))["t"] or Decimal(0)
            if already + self.amount > self.expense.amount:
                remaining = self.expense.amount - already
                raise ValidationError(
                    f"Refund exceeds what's left to refund on this expense "
                    f"(at most {remaining:,.2f}).")
            if self.date and self.expense.date and self.date < self.expense.date:
                raise ValidationError("A refund can't pre-date the expense it returns.")

    @property
    def department(self):
        return self.expense.department


class RemittanceBatch(models.Model):
    """A batch of trust-fund remittances to the conference/field. Moves through
    DRAFT → APPROVED → REMITTED. Trust funds are a liability until the batch is
    marked remitted (its expense postings become PAID)."""
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        REMITTED = "REMITTED", "Remitted"
        CANCELLED = "CANCELLED", "Cancelled"

    batch_number = models.CharField(max_length=20, unique=True, editable=False)
    date = models.DateField(auto_now_add=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # The payment instrument that settles this remittance liability (cheque, EFT,
    # RTGS, M-Pesa, etc.). A batch cannot be marked sent until one is issued.
    payment = models.ForeignKey("cashbook.PaymentInstrument", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="settled_remittance")
    # Legacy cheque fields — retained for historical data; superseded by `payment`.
    cheque_no = models.CharField(max_length=30, blank=True)
    cheque_date = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                   related_name="remittance_batches")
    approved_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="remittance_approvals")
    remitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.batch_number

    @property
    def is_settled(self):
        """True once an issued payment instrument is linked (the obligation is
        being settled), regardless of whether it has cleared yet."""
        return self.payment_id is not None and self.payment.status in (
            "ISSUED", "OUTSTANDING", "CLEARED")

    @property
    def settlement_label(self):
        if self.payment_id:
            p = self.payment
            return f"{p.get_method_display()} {p.instrument_number}".strip()
        if self.cheque_no:                      # legacy, pre-migration
            return f"Cheque {self.cheque_no}"
        return ""

    @staticmethod
    def next_number():
        import datetime as dt
        year = dt.date.today().year
        prefix = f"RB-{year}-"
        last = (RemittanceBatch.objects.filter(batch_number__startswith=prefix)
                .order_by("-batch_number").first())
        seq = (int(last.batch_number.split("-")[-1]) + 1) if last else 1
        return f"{prefix}{seq:04d}"

    @classmethod
    def create_batch(cls, **kwargs):
        """Allocate a unique batch number and create the batch atomically. Locks
        the year's rows (PostgreSQL) and retries on the rare collision so two
        concurrent remittances can't claim the same number / raise IntegrityError."""
        import datetime as dt
        from django.db import IntegrityError, transaction as _tx
        prefix = f"RB-{dt.date.today().year}-"
        last_err = None
        for _ in range(8):
            try:
                with _tx.atomic():
                    last = (cls.objects.select_for_update()
                            .filter(batch_number__startswith=prefix)
                            .order_by("-batch_number").first())
                    seq = (int(last.batch_number.split("-")[-1]) + 1) if last else 1
                    number = f"{prefix}{seq:04d}"
                    return cls.objects.create(batch_number=number, **kwargs)
            except IntegrityError as exc:        # another worker took this number
                last_err = exc
                continue
        raise last_err or IntegrityError("Could not allocate a remittance batch number.")

    def recompute_total(self):
        from django.db.models import Sum
        self.total_amount = self.expenses.aggregate(t=Sum("amount"))["t"] or 0
        return self.total_amount


class FundTransfer(models.Model):
    """A movement of money between two of the church's own funds. It is neither
    income nor expenditure — it leaves total funds unchanged but moves the balance
    from one fund to another. Trust funds are excluded (that money is restricted
    and must be remitted, not reallocated)."""
    date = models.DateField(db_index=True)
    source = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                               related_name="transfers_out")
    destination = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                    related_name="transfers_in")
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    reason = models.CharField(max_length=200, blank=True)
    reference = models.CharField(max_length=40, blank=True)
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="transfers_recorded")
    is_reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True, blank=True)
    is_reversal = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [models.Index(fields=["date"])]

    def __str__(self):
        return f"{self.amount} {self.source} → {self.destination}"

    @property
    def is_locked(self):
        """A transfer can't be edited once it has been reversed, is itself a
        reversal entry, or falls inside a locked accounting period."""
        from core.models import period_locked
        if self.is_reversed or self.is_reversal:
            return True
        if self.date and period_locked(self.date):
            return True
        return False

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.source_id and self.destination_id and self.source_id == self.destination_id:
            raise ValidationError("Source and destination funds must be different.")
        if self.amount is not None and self.amount <= 0:
            raise ValidationError("Transfer amount must be greater than zero.")
        for fund, lbl in ((self.source, "source"), (self.destination, "destination")):
            if fund and fund.is_trust:
                raise ValidationError(
                    f"Trust funds cannot be used as the {lbl} of a transfer — "
                    "trust money is restricted and must be remitted to the field.")

    def reverse(self, user):
        """Post a mirror transfer (funds swapped) that backs this one out, keeping
        both for the audit trail. Never deletes."""
        from django.utils import timezone
        if self.is_reversed:
            raise ValueError("This transfer has already been reversed.")
        if self.is_reversal:
            raise ValueError("A reversal entry cannot itself be reversed.")
        mirror = FundTransfer.objects.create(
            date=self.date, source=self.destination, destination=self.source,
            amount=self.amount, reason=f"[Reversal of transfer #{self.pk}] {self.reason}".strip(),
            reference=self.reference, recorded_by=user, is_reversal=True)
        self.is_reversed = True
        self.reversed_at = timezone.now()
        self.save(update_fields=["is_reversed", "reversed_at"])
        return mirror


class RecurringExpense(models.Model):
    """A predetermined expense paid on a regular cadence — every Sabbath or every
    month (e.g. an allowance, a weekly stipend). The schedule itself doesn't touch
    balances; it *generates* ordinary Expense records on their due dates, which then
    flow through approval, the ledger and every report like any other expense."""
    class Frequency(models.TextChoices):
        SABBATH = "SABBATH", "Every Sabbath (weekly)"
        MONTHLY = "MONTHLY", "Every month"
        QUARTERLY = "QUARTERLY", "Every quarter"
        YEARLY = "YEARLY", "Every year"

    description = models.CharField(max_length=200)
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="recurring_expenses")
    category = models.CharField(max_length=14, choices=Expense.Category.choices,
                                default=Expense.Category.ALLOWANCE)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    frequency = models.CharField(max_length=10, choices=Frequency.choices,
                                 default=Frequency.MONTHLY)
    day_of_month = models.PositiveSmallIntegerField(
        default=1, help_text="For monthly/quarterly/yearly schedules: day of the month the payment falls due.")
    claimant = models.CharField(max_length=120, blank=True)
    # --- everything else an Expense carries -------------------------------
    # A schedule that cannot record a supplier, a payee or a budget line
    # produces expenses missing exactly the details a treasurer would have
    # filled in by hand — so the generated rows had to be edited afterwards,
    # which defeats the point of scheduling them. These mirror Expense field
    # for field, and `services.recurring` copies them onto each generated row.
    expenditure_type = models.CharField(
        max_length=12, choices=Expense.ExpenditureType.choices,
        default=Expense.ExpenditureType.RECURRENT,
        help_text="A scheduled cost is recurrent by nature, but a monthly "
                  "instalment on a capital purchase is not.")
    vendor = models.ForeignKey(
        "vendors.Vendor", null=True, blank=True, on_delete=models.PROTECT,
        related_name="recurring_expenses",
        help_text="The supplier, where they are on the register. Every payment "
                  "this schedule generates lands on their account.")
    payee = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Filled from the supplier if left blank.")
    voucher_no = models.CharField(
        max_length=30, blank=True, default="",
        help_text="A fixed reference, e.g. a standing order number. Left blank "
                  "for most schedules, since each payment gets its own.")
    paid_from_petty_cash = models.BooleanField(default=False)
    budget_line = models.ForeignKey(
        "cashbook.BudgetLine", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="recurring_expenses")
    method = models.CharField(max_length=8, choices=Expense.Method.choices,
                              default=Expense.Method.CASH)
    start_date = models.DateField(help_text="First date the schedule is effective from.")
    end_date = models.DateField(null=True, blank=True,
                                help_text="Optional: stop generating after this date.")
    active = models.BooleanField(default=True)
    last_generated = models.DateField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                   related_name="recurring_expenses_created")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-active", "description"]

    def __str__(self):
        return f"{self.description} ({self.get_frequency_display()})"


class PettyCashTopUp(models.Model):
    """Cash placed into the petty cash float (a cash-location movement). It does not
    change any fund's balance — it just records physical cash set aside for petty
    payments. The float = sum of top-ups less petty-cash disbursements."""
    date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    note = models.CharField(max_length=200, blank=True)

    # The cheque (or transfer) that put this cash in the tin, where there was one.
    #
    # A church almost never tops up petty cash from thin air: somebody writes a
    # cheque payable to CASH, walks it to the bank, and brings the notes back.
    # Those are TWO movements — money leaves the bank, and money arrives in the
    # tin — and they must both be recorded or the books will not add up. Recording
    # only the top-up leaves the bank overstated; recording only the cheque leaves
    # the float understated.
    #
    # Linking them means the cheque is issued once, in the payments register where
    # every other cheque lives, and the float rises automatically when it is
    # issued. See `services.petty_cash.replenish_from_instrument`.
    instrument = models.OneToOneField(
        "cashbook.PaymentInstrument", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="petty_topup",
        help_text="The cheque or transfer that funded this top-up, if any.")

    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="petty_topups")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Petty cash top-up {self.amount} on {self.date}"


def expense_receipt_path(instance, filename):
    """Store receipts under the YEAR/MONTH the expense was INCURRED (not uploaded),
    so a year's supporting documents sit together for audit/printing."""
    d = getattr(getattr(instance, "expense", None), "date", None)
    if d:
        return f"receipts/expenses/{d:%Y}/{d:%m}/{filename}"
    return f"receipts/expenses/unknown/{filename}"


def clean_receipt_text(text):
    """Strip configured boilerplate phrases (Settings → 'strings to remove from
    receipt messages') from a pasted bank/M-Pesa message — e.g. the 'never share
    your PIN' warning banks append to every SMS — then tidy leftover whitespace.

    A phrase is normally matched literally, EXCEPT that any run of whitespace in
    the configured phrase matches any run of whitespace in the message (a single
    space matches a double space and vice versa) — receipts are often re-typed
    or copy-pasted with slightly different spacing, and a phrase that looks
    identical to the eye but differs by one space would otherwise silently fail
    to match at all.

    Use `*` as a wildcard for parts that change every time (an amount, a
    balance, a link code): it greedily matches any run of characters up to
    the next fixed part of the phrase. For example
        New M-PESA balance is Ksh*. Transaction cost, Ksh*.
    strips that whole sentence regardless of what the actual figures are —
    including when an amount itself contains a period, like "8,376.00".
    """
    import re
    if not text:
        return text
    from core.models import SiteConfig
    try:
        raw = SiteConfig.get().receipt_strip_strings or ""
    except Exception:  # noqa: BLE001 — settings must never block a save
        return text
    for phrase in (p.strip() for p in raw.splitlines()):
        if not phrase:
            continue
        # tokenize on "*" (wildcard) and runs of whitespace (space-tolerant),
        # escaping every other literal chunk, then rebuild as a single regex.
        # The wildcard is GREEDY (not "*?"): a non-greedy match stops at the
        # *first* occurrence of the next literal character, which breaks on
        # an amount like "499,900.00" followed by a literal "." — it would
        # stop at the internal decimal point instead of the sentence's own
        # full stop. Greedy correctly consumes the whole number.
        tokens = re.split(r"(\*|\s+)", phrase)
        pattern = "".join(
            r".*" if tok == "*" else
            r"\s+" if tok and tok.isspace() else
            re.escape(tok)
            for tok in tokens if tok)
        try:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        except re.error:
            continue   # a malformed phrase must never break the save
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class ExpenseAttachment(models.Model):
    """A receipt or supporting document attached to an expense (e.g. a claimant's
    receipt brought back after an advance)."""
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=expense_receipt_path, blank=True, null=True)
    text = models.TextField(blank=True,
        help_text="A text receipt, e.g. a pasted M-Pesa confirmation message.")
    link = models.URLField(blank=True, help_text="A link to an online/e-receipt.")
    label = models.CharField(max_length=120, blank=True)
    uploaded_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def save(self, *args, **kwargs):
        # scrub configured boilerplate from pasted messages on every save, so
        # every entry path (treasurer, leader, queue, advance) is covered
        if self.text:
            self.text = clean_receipt_text(self.text)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.label or self.file.name


class SettleableObligation(models.Model):
    """Something the church owes that may be discharged over several payments.

    Payables and accruals are different animals — a payable is an invoice that
    has arrived, an accrual is an estimate of one that has not — but they are
    settled identically: by payments, in instalments, until the balance clears.
    That behaviour is defined once here rather than twice, so there is one
    implementation of "how much do we still owe" to reason about and one place
    a rounding or status rule can be got wrong.

    A subclass supplies a `payments` reverse relation (a ForeignKey from
    Expense) and the `amount` it owes. Everything else follows.
    """

    #: Payments count once they are APPROVED or PAID — the same test
    #: StaffAdvance.settled_total applies, so a pending claim does not reduce a
    #: liability before anyone has authorised it.
    COUNTED_STATUSES = (Expense.Status.APPROVED, Expense.Status.PAID)

    class Meta:
        abstract = True

    def paid_asof(self, on=None):
        qs = self.payments.filter(status__in=self.COUNTED_STATUSES)
        if on is not None:
            qs = qs.filter(date__lte=on)
        return qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")

    @property
    def paid_total(self):
        return self.paid_asof()

    def _flag_settled_asof(self, on=None):
        """Whether the cached `settled` flag says this was discharged by `on`.

        Reads the flag alone — what the payments say is the caller's business
        (see `balance_asof`). As at a date, a settlement that had not happened
        yet cannot count; "right now" trusts the flag outright, exactly as the
        balance-sheet query's no-date path does.
        """
        if not self.settled:
            return False
        if on is None:
            return True
        return self.settled_on is not None and self.settled_on <= on

    def balance_asof(self, on=None):
        """What is still owed. Never negative: an overpayment is a matter for
        the supplier's account, not a negative liability on the balance sheet.

        An obligation flagged settled with NO payment to show for it owes
        nothing. That is how every settlement made before instalments existed
        looks when its expense link was never recorded, and the flag a
        treasurer set is the only evidence there is. This rule already governs
        the balance sheet (`treasury_position._open_obligation_total`); it
        lives here as well so that the read path ("what do we owe?") and the
        write path ("may this be paid again?") cannot answer differently about
        the same row — the gap that let `settle()` take a second full payment
        on a debt already discharged.

        Narrow on purpose: the flag is believed only where there is no payment
        evidence at all. Once payments exist they are the better record and the
        arithmetic decides, so a flag can never override a real figure.
        """
        paid = self.paid_asof(on)
        if not paid and self._flag_settled_asof(on):
            return Decimal("0")
        return max(self.amount - paid, Decimal("0"))

    @property
    def balance(self):
        return self.balance_asof()

    @property
    def is_settled(self):
        # Nothing left owed — by payments, or by a flag-only settlement that
        # `balance_asof` recognises. Derived from the one figure rather than
        # recomputed, so "settled" and "owes nothing" cannot drift apart.
        return self.balance_asof() <= 0

    @property
    def is_part_paid(self):
        return not self.is_settled and self.paid_total > 0

    @property
    def status_label(self):
        if self.is_settled:
            return "Settled"
        if self.is_part_paid:
            return "Part paid"
        return "Outstanding"

    @property
    def percent_paid(self):
        if not self.amount:
            return 0
        return int(min(self.paid_total / self.amount * 100, 100))

    @property
    def settled_expense(self):
        """The instalment that cleared the balance.

        A read-only property because this used to be a real OneToOne field and
        callers still ask for it. It now means "the payment that finished the
        job" rather than "the payment", which is the only sensible reading once
        there can be several.
        """
        if not self.is_settled:
            return None
        return (self.payments.filter(status__in=self.COUNTED_STATUSES)
                .order_by("date", "id").last())


class Payable(SettleableObligation):
    """An amount owed for goods/services received but not yet paid (a credit
    purchase). Tracked as an obligation; settling it records the actual payment."""
    date = models.DateField(db_index=True, help_text="Date the liability was incurred.")
    vendor = models.CharField(
        max_length=120,
        help_text="The supplier's name as it appears on the invoice. Kept even "
                  "when `supplier` is set — it is what the document said.")
    supplier = models.ForeignKey(
        "vendors.Vendor", null=True, blank=True, on_delete=models.PROTECT,
        related_name="payables",
        help_text="The supplier register entry, where there is one.")
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="payables")
    category = models.CharField(max_length=14, choices=Expense.Category.choices,
                                default=Expense.Category.OTHER)
    due_date = models.DateField(null=True, blank=True)
    # `settled` and `settled_on` are a CACHE of what the payments say, not a
    # second opinion. They exist because "show me what we still owe" is a
    # filter on thousands of rows and must stay indexed, and because reports,
    # the backup export and the settle-from-expense form already read them.
    # They are only ever written by services.obligations.refresh_settlement(),
    # which derives them from the payments — never set by hand.
    settled = models.BooleanField(default=False, db_index=True)
    settled_on = models.DateField(
        null=True, blank=True,
        help_text="The date the FINAL instalment cleared the balance.")
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="payables_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["settled", "due_date", "-date"]

    def __str__(self):
        return f"{self.vendor}: {self.amount}"


class Accrual(SettleableObligation):
    """An expense incurred in a period but not yet invoiced/paid (e.g. an estimate
    for utilities consumed). A liability until settled."""
    date = models.DateField(db_index=True, help_text="Period-end the accrual relates to.")
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="accruals")
    category = models.CharField(max_length=14, choices=Expense.Category.choices,
                                default=Expense.Category.OTHER)
    # A cache of what the payments say, written only by
    # services.obligations.refresh_settlement(). See SettleableObligation.
    settled = models.BooleanField(default=False, db_index=True)
    settled_on = models.DateField(
        null=True, blank=True,
        help_text="The date the FINAL instalment cleared the balance.")
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="accruals_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["settled", "-date"]

    def __str__(self):
        return f"{self.description}: {self.amount}"


class Prepayment(models.Model):
    """Cash paid in advance for benefits spanning future periods (e.g. an annual
    insurance premium). The unexpired portion is a current asset."""
    date = models.DateField(db_index=True, help_text="Date paid.")
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="prepayments")
    category = models.CharField(max_length=14, choices=Expense.Category.choices,
                                default=Expense.Category.OTHER)
    months = models.PositiveSmallIntegerField(default=12,
        help_text="Number of months the prepayment is spread over.")
    start_date = models.DateField(help_text="First month the benefit applies.")
    source_expense = models.ForeignKey(Expense, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="prepayments")
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="prepayments_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date"]

    def unexpired(self, as_of=None):
        """Unexpired (asset) portion as at a date, straight-line by month."""
        import datetime as _dt
        from decimal import Decimal as _D
        as_of = as_of or _dt.date.today()
        if self.months <= 0:
            return _D(0)
        months_elapsed = ((as_of.year - self.start_date.year) * 12
                          + (as_of.month - self.start_date.month))
        months_elapsed = max(0, min(months_elapsed, self.months))
        per = (self.amount / self.months)
        return (self.amount - per * months_elapsed).quantize(_D("0.01"))

    def __str__(self):
        return f"{self.description}: {self.amount}"


class StaffAdvance(models.Model):
    """A cash advance (imprest) issued to a member of staff before they incur
    expenses, e.g. an advance for a conference trip. The advance is later settled
    by linking the actual expenses to it; any surplus is recovered and any
    shortfall reimbursed."""
    class Status(models.TextChoices):
        ISSUED = "ISSUED", "Issued"
        PARTLY = "PARTLY", "Partly settled"
        SETTLED = "SETTLED", "Settled"
        CLOSED = "CLOSED", "Closed"

    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK = "BANK", "Bank"
        CHEQUE = "CHEQUE", "Cheque"
        MPESA = "MPESA", "M-Pesa"

    staff_name = models.CharField(max_length=120)
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="advances")
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    date_issued = models.DateField()
    purpose = models.CharField(max_length=200)
    method = models.CharField(max_length=8, choices=Method.choices, default=Method.CASH)
    from_petty_cash = models.BooleanField(default=False,
        help_text="Issued out of the petty-cash float (reduces petty cash; the "
                  "advance is a receivable until accounted for).")
    returned_to_petty = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        help_text="Unspent cash the staff member returned to the petty-cash box.")
    bank_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        help_text="Bank / M-Pesa transaction charge incurred to issue this advance.")
    charge_expense = models.OneToOneField("cashbook.Expense", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="advance_charge",
        help_text="The expense that records this advance's bank/M-Pesa charge.")
    reference = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ISSUED,
                              db_index=True)
    issued_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                  related_name="advances_issued")
    settled_on = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date_issued", "-id"]

    def __str__(self):
        return f"Advance to {self.staff_name}: {self.amount}"

    @property
    def settled_total(self):
        from django.db.models import Sum
        return self.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID]
        ).aggregate(t=Sum("amount"))["t"] or Decimal(0)

    def settled_asof(self, on):
        from django.db.models import Sum
        return self.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
            date__lte=on).aggregate(t=Sum("amount"))["t"] or Decimal(0)

    @property
    def awaiting_approval_total(self):
        """Receipts handed in against this advance that nobody has approved yet.

        `settled_total` counts only approved and paid receipts, which is right:
        an unapproved receipt has not been accepted as accounting for anything,
        and counting it would let an advance look settled before a single one
        had been read.

        But the advance page lists *every* receipt attached to it, so a holder
        who has handed in more than has been approved sees a list totalling one
        figure and a "settled by receipts" card showing a smaller one, with
        nothing to say why. This is that difference, so the page can show it and
        say what to do about it rather than leaving a treasurer to work out
        whether they are looking at a bug or a backlog.
        """
        from django.db.models import Sum
        return self.expenses.filter(
            status=Expense.Status.PENDING
        ).aggregate(t=Sum("amount"))["t"] or Decimal(0)

    @property
    def receipts_submitted_total(self):
        """Everything handed in, approved or not — what the page's list adds to."""
        return self.settled_total + self.awaiting_approval_total

    @property
    def balance(self):
        """Positive = surplus to recover from staff; negative = shortfall owed to staff."""
        return self.amount - self.settled_total - (self.returned_to_petty or Decimal(0))

    @property
    def accounted_total(self):
        """Everything accounted for: expenses settled + any cash returned."""
        return self.settled_total + (self.returned_to_petty or Decimal(0))

    @property
    def topups_total(self):
        from django.db.models import Sum
        return self.topups.aggregate(t=Sum("amount"))["t"] or Decimal(0)

    @property
    def base_amount(self):
        """The originally issued amount (total advanced less later top-ups)."""
        return (self.amount or Decimal(0)) - self.topups_total

    def petty_cash_out_asof(self, on):
        """For a petty-cash-funded advance: cash that has physically left the
        petty-cash box and not yet been physically returned to it, as of a
        date. The base issue leaves the box on date_issued and each top-up on
        its own date; only an actual return (returned_to_petty) brings cash
        back into the box. Recording an expense that accounts for how the
        advance was spent does NOT return any cash to the box — it's a
        paperwork reclassification, not a cash movement — so it must NOT
        affect this figure. This is the number the petty cash float's own
        running balance depends on (see _petty_balance_asof); it is
        deliberately a different figure from petty_outstanding_asof (below),
        which answers a different question."""
        if not self.from_petty_cash:
            return Decimal(0)
        out = Decimal(0)
        if self.date_issued <= on:
            out += self.base_amount
        for t in self.topups.all():
            if t.date <= on:
                out += t.amount
        # The return has no date of its own; the date the cash came back is
        # `settled_on`, which is what the petty cash register uses when it
        # credits the box. This used to subtract the return with no date test at
        # all, while the issue and the top-ups above were both date-gated — so
        # an advance that had been returned showed as never having left the box,
        # at any as-of date, including dates before the money went out. Where
        # the whole advance came back the two cancelled exactly and the float
        # card simply did not acknowledge the advance, while the register did.
        if self.returned_to_petty and self.settled_on and self.settled_on <= on:
            out -= self.returned_to_petty
        # Deliberately not clamped at zero. A return larger than the cash issued
        # is a data error, and the register would carry it into the balance; if
        # this clamped, the two would disagree again and the error would be
        # hidden in the one place it is most visible.
        return out

    def petty_outstanding_asof(self, on):
        """For a petty-cash-funded advance: cash that has left the petty-cash
        box and not yet been accounted for. Starts from the same cash-out
        figure as petty_cash_out_asof (above), but additionally subtracts any
        expense recorded against the advance (once approved or paid) — since
        that accounts for that portion of it, even though no cash physically
        returned to the box. Without this, the reconciliation's "not yet
        accounted for" line always showed the full amount ever disbursed,
        never decreasing as expenses were recorded, even for an advance fully
        accounted for down to zero. Used for reconciliation/reporting
        purposes only — never for the petty cash float's own balance, which
        must not change just because an advance was accounted for on paper."""
        if not self.from_petty_cash:
            return Decimal(0)
        out = self.petty_cash_out_asof(on)
        settled = (self.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID], date__lte=on)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        out -= settled
        return out if out > 0 else Decimal(0)


class AdvanceTopUp(models.Model):
    """Additional cash issued onto an existing open advance — e.g. the holder had
    a small unspent balance and needs more for further payments, so rather than
    retiring and re-issuing, the advance is topped up. The parent advance's
    `amount` is the running total (base issue + all top-ups)."""
    advance = models.ForeignKey(StaffAdvance, on_delete=models.CASCADE,
                                related_name="topups")
    date = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    charge = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        help_text="Bank/M-Pesa charge for sending this top-up — the church's "
                  "own cost, booked as an expense but not added to what the "
                  "advance holder must account for.")
    charge_expense = models.OneToOneField("cashbook.Expense", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="topup_charge_for")
    note = models.CharField(max_length=200, blank=True)
    issued_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["date", "id"]


class ExpenseCategory(models.Model):
    """A church-defined expense category that supplements the built-in ones, so
    categories can be changed without code edits."""
    code = models.CharField(max_length=20, unique=True)
    label = models.CharField(max_length=80)
    active = models.BooleanField(default=True)
    sort = models.PositiveIntegerField(default=100)
    is_liability = models.BooleanField(default=False,
        help_text="Tick for liability-settlement categories (deposit refunds, "
                  "advance settlements, deferred income, …). Vouchers in this "
                  "category file under the Liability Register instead of "
                  "Expenses — no code change needed for new liability types.")

    class Meta:
        ordering = ["sort", "label"]

    def __str__(self):
        return self.label


# Built-in categories that are liability settlements, not operational spend.
# Custom categories add to this set via ExpenseCategory.is_liability — new
# liability types (deposit refunds, advance settlements, deferred income, …)
# therefore need no code change.
_LIABILITY_BUILTIN = frozenset({"REMITTANCE", "LOAN_REPAYMENT"})


def classify_category(code):
    """The DocClass a category belongs to. Single source of truth for the
    expense/liability split — Expense.save() derives doc_class from this, so
    every creation path (forms, services, imports, remittance batches, loan
    contras) is classified consistently without touching call sites."""
    if not code:
        return Expense.DocClass.EXPENSE
    if code in _LIABILITY_BUILTIN:
        return Expense.DocClass.LIABILITY
    try:
        ec = ExpenseCategory.objects.filter(code=code).only("is_liability").first()
        if ec and ec.is_liability:
            return Expense.DocClass.LIABILITY
    except Exception:  # noqa: BLE001 — table may not exist mid-migration
        pass
    return Expense.DocClass.EXPENSE


def category_choices():
    """Built-in categories plus any active custom ones (for form dropdowns)."""
    choices = list(Expense.Category.choices)
    seen = {c for c, _ in choices}
    for ec in ExpenseCategory.objects.filter(active=True):
        if ec.code not in seen:
            choices.append((ec.code, ec.label))
    return choices


def category_label(code):
    """Resolve a category code to its label (built-in or custom)."""
    if not code:
        return ""
    d = dict(Expense.Category.choices)
    if code in d:
        return d[code]
    ec = ExpenseCategory.objects.filter(code=code).first()
    return ec.label if ec else code


class RemittanceDeadline(models.Model):
    """A trust-fund remittance deadline for a given period (usually monthly), used
    to drive the remittance calendar. If a deadline falls on a non-Sabbath day,
    the reporting Sabbath is the most recent Saturday on or before the deadline —
    i.e. the last counted Sabbath whose money must be in the remittance.

    Deadlines for a year can be auto-generated (one per month) and then adjusted.
    """
    year = models.PositiveIntegerField(db_index=True)
    period_month = models.PositiveSmallIntegerField(
        help_text="Calendar month this deadline closes (1–12).")
    label = models.CharField(max_length=60, blank=True,
                             help_text="e.g. 'January remittance'.")
    deadline = models.DateField(help_text="The date the remittance is due to the field/conference.")
    notes = models.CharField(max_length=200, blank=True)
    remitted = models.BooleanField(default=False,
        help_text="Tick once this period's remittance has been sent.")
    batch = models.ForeignKey("cashbook.RemittanceBatch", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="deadlines")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["year", "period_month", "deadline"]
        unique_together = [("year", "period_month")]

    def __str__(self):
        return f"{self.label or self.get_period_display()} — due {self.deadline:%d %b %Y}"

    def get_period_display(self):
        import calendar
        return f"{calendar.month_name[self.period_month]} {self.year}"

    @property
    def reporting_sabbath(self):
        """The Sabbath whose count this remittance reports on: the most recent
        Saturday on or before the deadline."""
        from core.utils import last_saturday
        return last_saturday(self.deadline)

    @property
    def deadline_is_sabbath(self):
        return self.deadline.weekday() == 5

    @property
    def days_until(self):
        import datetime as dt
        return (self.deadline - dt.date.today()).days

    @property
    def is_overdue(self):
        return (not self.remitted) and self.days_until < 0

    @property
    def is_due_soon(self):
        return (not self.remitted) and 0 <= self.days_until <= 7


class BudgetLine(models.Model):
    """A named budget item for a fund in a year — e.g. for a Camp Meeting fund:
    "Accommodation" 50,000 · "Catering" 30,000 · "Pulpit / honoraria" 20,000.
    Expenses can be tagged with the specific item they relate to (Expense.budget_line)
    so we can report actual spend per item. The expense's own `category` is kept
    separate, for the overall expense categorisation. `category` here is an
    optional default suggested when the item is chosen on the expense form."""
    department = models.ForeignKey("departments.Department", on_delete=models.CASCADE,
                                   related_name="budget_lines")
    year = models.PositiveIntegerField(db_index=True)
    name = models.CharField(max_length=80, default="")
    category = models.CharField(max_length=14, choices=Expense.Category.choices,
                                blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        unique_together = [("department", "year", "name")]
        ordering = ["department", "year", "name"]

    def __str__(self):
        return f"{self.department} {self.year} {self.name}: {self.amount}"
        return f"{self.department} {self.year} {self.get_category_display()}: {self.amount}"


class ChequeRegister(models.Model):
    """Tracks cheques the church has issued, so the treasurer can reconcile them
    against the bank statement. A cheque is 'unpresented' until it clears; the
    bank reconciliation can subtract the still-unpresented cheques automatically.
    Each entry may be linked to the expense or remittance batch it paid."""
    class Status(models.TextChoices):
        ISSUED = "ISSUED", "Issued (unpresented)"
        CLEARED = "CLEARED", "Cleared"
        BOUNCED = "BOUNCED", "Bounced"
        CANCELLED = "CANCELLED", "Cancelled / void"

    cheque_number = models.CharField(max_length=40, db_index=True)
    payee = models.CharField(max_length=160, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    date_issued = models.DateField(db_index=True)
    date_cleared = models.DateField(null=True, blank=True,
        help_text="Date the cheque cleared or bounced at the bank.")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.ISSUED, db_index=True)
    expense = models.ForeignKey("cashbook.Expense", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="cheques")
    remittance_batch = models.ForeignKey("cashbook.RemittanceBatch", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="cheques")
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey("auth.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="cheques_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date_issued", "-id"]
        indexes = [models.Index(fields=["status", "date_issued"])]

    def __str__(self):
        return f"Cheque {self.cheque_number} — {self.amount} ({self.get_status_display()})"

    @property
    def is_unpresented(self):
        return self.status == self.Status.ISSUED


class PaymentInstrument(models.Model):
    """A payment instrument the church issues to settle an existing accounting
    obligation — a cheque today, but the same framework supports EFT, RTGS,
    M-Pesa and other methods.

    Crucially this is *not* an accounting transaction on its own. The underlying
    source (an expense voucher, a trust-fund remittance, a refund, or a fund
    transfer) is what posts to the ledger. The instrument only records HOW that
    obligation is being paid and tracks its clearing status, so it never creates
    duplicate journal entries. Bank reconciliation simply flips an issued
    instrument to Cleared."""

    class Method(models.TextChoices):
        CHEQUE = "CHEQUE", "Cheque"
        EFT = "EFT", "EFT (bank transfer)"
        RTGS = "RTGS", "RTGS"
        MPESA = "MPESA", "M-Pesa"
        CASH = "CASH", "Cash"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        PREPARED = "PREPARED", "Prepared"
        ISSUED = "ISSUED", "Issued"
        OUTSTANDING = "OUTSTANDING", "Outstanding"
        PRESENTED = "PRESENTED", "Presented"
        CLEARED = "CLEARED", "Cleared"
        CANCELLED = "CANCELLED", "Cancelled"
        REJECTED = "REJECTED", "Rejected"
        VOIDED = "VOIDED", "Voided"
        REVERSED = "REVERSED", "Reversed"
        EXPIRED = "EXPIRED", "Expired"
        STOPPED = "STOPPED", "Stopped"      # legacy alias of Cancelled

    class SourceKind(models.TextChoices):
        EXPENSE = "EXPENSE", "Expense voucher"
        REMITTANCE = "REMITTANCE", "Trust fund remittance"
        REFUND = "REFUND", "Refund"
        TRANSFER = "TRANSFER", "Fund transfer"
        SUPPLIER = "SUPPLIER", "Supplier payment"
        PETTY_CASH = "PETTY_CASH", "Petty cash replenishment"
        MANUAL = "MANUAL", "Manual / standalone"

    # states still outstanding at the bank (not yet cleared, not cancelled)
    OUTSTANDING_STATES = ("ISSUED", "OUTSTANDING", "PRESENTED")
    # terminal, never-cleared states (the instrument will not hit the bank)
    TERMINAL_STATES = ("CANCELLED", "REJECTED", "VOIDED", "REVERSED",
                       "EXPIRED", "STOPPED")
    # states whose details are locked (cannot be edited or deleted)
    LOCKED_STATES = ("CLEARED",)
    # methods that clear through the bank (everything except cash in hand)
    BANK_CLEARING_METHODS = ("CHEQUE", "EFT", "RTGS", "MPESA", "OTHER")

    method = models.CharField(max_length=8, choices=Method.choices,
                              default=Method.CHEQUE, db_index=True)
    instrument_number = models.CharField(max_length=40, blank=True, db_index=True,
        help_text="Cheque number, EFT/RTGS reference, or M-Pesa code.")
    payee = models.CharField(max_length=160, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    bank_account = models.ForeignKey("statements.BankAccount", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="payment_instruments")

    # Each business event keeps its own date — they never overwrite each other.
    date_prepared = models.DateField(null=True, blank=True)
    date_issued = models.DateField(null=True, blank=True, db_index=True)
    date_payment = models.DateField(null=True, blank=True,
        help_text="Value/payment date if different from the issue date.")
    date_presented = models.DateField(null=True, blank=True)
    # THE critical reconciliation field: the date the instrument actually
    # cleared the bank per the statement. Historical reconciliations test
    # `date_cleared <= reconciliation date`, never the current status, so a
    # cheque issued 5 Jul that cleared 19 Jul still shows OUTSTANDING on a
    # 10 Jul reconciliation even when today's status is Cleared.
    date_cleared = models.DateField(null=True, blank=True, db_index=True)
    date_cancelled = models.DateField(null=True, blank=True)
    date_voided = models.DateField(null=True, blank=True)
    date_reversed = models.DateField(null=True, blank=True)
    # the imported bank debit that cleared this instrument (reconciliation
    # trail + the guard that stops the same debit clearing two instruments)
    bank_transaction = models.ForeignKey("giving.Transaction", null=True,
        blank=True, on_delete=models.SET_NULL, related_name="cleared_instruments")

    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.DRAFT, db_index=True)

    # --- source obligation (exactly one, unless a permitted manual payment) ---
    source_kind = models.CharField(max_length=12, choices=SourceKind.choices,
                                   default=SourceKind.EXPENSE)
    expense = models.ForeignKey("cashbook.Expense", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="payments")
    # one EFT/RTGS may settle several vouchers at once: the primary expense
    # stays on `expense`, the rest attach here; validation checks the total
    extra_expenses = models.ManyToManyField(
        "cashbook.Expense", blank=True, related_name="covering_payments")
    remittance_batch = models.ForeignKey("cashbook.RemittanceBatch", null=True,
        blank=True, on_delete=models.SET_NULL, related_name="payments")
    refund = models.ForeignKey("cashbook.ExpenseRefund", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="payments")
    transfer = models.ForeignKey("cashbook.FundTransfer", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="payments")

    # --- approval & dual signatories ---
    approved_by = models.ForeignKey("auth.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="payments_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    signatory_1 = models.CharField(max_length=120, blank=True)
    signatory_2 = models.CharField(max_length=120, blank=True)

    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey("auth.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="payments_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date_issued", "-id"]
        indexes = [models.Index(fields=["status", "date_issued"]),
                   models.Index(fields=["method", "status"])]

    def __str__(self):
        return f"{self.get_method_display()} {self.instrument_number} — {self.amount}"

    # ---- source resolution ----
    @property
    def source(self):
        return {self.SourceKind.EXPENSE: self.expense,
                self.SourceKind.REMITTANCE: self.remittance_batch,
                self.SourceKind.REFUND: self.refund,
                self.SourceKind.TRANSFER: self.transfer}.get(self.source_kind)

    @property
    def source_label(self):
        s = self.source
        if self.source_kind == self.SourceKind.MANUAL:
            return "Manual / standalone"
        if self.source_kind == self.SourceKind.SUPPLIER:
            return f"Supplier: {self.payee}" if self.payee else "Supplier payment"
        return str(s) if s else self.get_source_kind_display()

    @classmethod
    def outstanding_asof(cls, as_of, qs=None):
        """Instruments outstanding at the bank AS AT a date — the heart of
        historical bank reconciliation. An instrument is outstanding at
        `as_of` when it had been issued by then and had not yet cleared or
        been cancelled/voided/reversed BY THEN, judged by the event DATES —
        never by today's status. A cheque issued 1 Jul that cleared 19 Jul is
        outstanding on a 10 Jul reconciliation and cleared on a 31 Jul one,
        automatically, whenever the reconciliation is run.

        Legacy rows recorded before per-event dates existed fall back to their
        status (a CLEARED row with no cleared date is treated as always
        cleared), so historical reconciliation totals are preserved."""
        from django.db.models import Q
        qs = qs if qs is not None else cls.objects.all()
        qs = qs.filter(date_issued__isnull=False, date_issued__lte=as_of)
        qs = qs.exclude(status=cls.Status.DRAFT)
        # cleared by then — or legacy cleared with no date recorded
        qs = qs.exclude(Q(date_cleared__isnull=False, date_cleared__lte=as_of)
                        | Q(status=cls.Status.CLEARED, date_cleared__isnull=True))
        # cancelled / voided / reversed by then — or legacy terminal, no date
        for field in ("date_cancelled", "date_voided", "date_reversed"):
            qs = qs.exclude(**{f"{field}__isnull": False, f"{field}__lte": as_of})
        qs = qs.exclude(Q(status__in=cls.TERMINAL_STATES),
                        Q(date_cancelled__isnull=True),
                        Q(date_voided__isnull=True),
                        Q(date_reversed__isnull=True))
        return qs

    @property
    def is_outstanding(self):
        return self.status in self.OUTSTANDING_STATES

    @property
    def clearance_days(self):
        """Days from issue to bank clearance (None while outstanding)."""
        if self.date_issued and self.date_cleared:
            return (self.date_cleared - self.date_issued).days
        return None

    @property
    def all_expenses(self):
        """The expense voucher(s) this instrument settles (primary + extras)."""
        out = [self.expense] if self.expense_id else []
        if self.pk:
            out += [e for e in self.extra_expenses.all()
                    if e.pk != self.expense_id]
        return out

    @property
    def fund_names(self):
        return ", ".join(sorted({e.department.name for e in self.all_expenses
                                 if e.department_id}))

    @property
    def is_locked(self):
        """Cleared instruments are immutable — reverse/void instead of editing."""
        return self.status in self.LOCKED_STATES

    @property
    def needs_source(self):
        return self.source_kind not in (self.SourceKind.MANUAL,
                                        self.SourceKind.SUPPLIER)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Amount must be greater than zero."})
        # every instrument must reference a source obligation, unless it is an
        # explicitly manual/supplier payment (which the view gates on permission)
        if self.needs_source and self.source is None:
            raise ValidationError(
                "A payment must reference its source "
                f"({self.get_source_kind_display()}).")
        # the linked source amount should match (guards against mis-linking).
        # Skipped for multi-expense payments (one EFT covering several
        # vouchers): extra_expenses are set after save so aren't visible here,
        # and the view validates the combined total explicitly. Only enforce
        # the single-expense ceiling when this instrument already has a pk and
        # no extra expenses attached.
        src = self.source
        src_amt = getattr(src, "amount", None) if src else None
        # `_covers_multiple` is a transient flag the register sets before
        # full_clean when this instrument covers several vouchers (the M2M
        # can't be read pre-save); the view validates the combined total.
        has_extras = getattr(self, "_covers_multiple", False) or (
            bool(self.pk) and self.extra_expenses.exists())
        if (src_amt is not None and self.source_kind == self.SourceKind.EXPENSE
                and not has_extras and self.amount > src_amt):
            raise ValidationError(
                {"amount": "Payment exceeds the linked expense amount."})

    # thin wrappers kept for backward compatibility; the audited entry point
    # is cashbook.services.payments.apply_event (records a PaymentEvent too)
    def approve(self, user, on=None, comment=""):
        from cashbook.services.payments import apply_event
        apply_event(self, "APPROVE", user, on=on, comment=comment)

    def issue(self, on=None, user=None, comment=""):
        from cashbook.services.payments import apply_event
        apply_event(self, "ISSUE", user, on=on, comment=comment)

    def clear(self, on=None, user=None, comment="", bank_transaction=None):
        from cashbook.services.payments import apply_event
        apply_event(self, "CLEAR", user, on=on, comment=comment,
                    bank_transaction=bank_transaction)

    def void(self, user=None, on=None, comment=""):
        from cashbook.services.payments import apply_event
        apply_event(self, "VOID", user, on=on, comment=comment)

    def stop(self, user=None, on=None, comment=""):
        from cashbook.services.payments import apply_event
        apply_event(self, "CANCEL", user, on=on, comment=comment)


class PaymentEvent(models.Model):
    """One lifecycle event on a payment instrument — the audit trail the
    register's timeline shows. Records who, when (both the business date and
    the wall clock), the status transition, a free comment and an optional
    reference (e.g. the bank row that cleared it). The instrument's current
    status is always the result of its latest lifecycle event."""

    class Event(models.TextChoices):
        CREATE = "CREATE", "Created"
        APPROVE = "APPROVE", "Approved"
        PREPARE = "PREPARE", "Prepared / printed"
        ISSUE = "ISSUE", "Issued"
        PRESENT = "PRESENT", "Presented"
        CLEAR = "CLEAR", "Cleared"
        CANCEL = "CANCEL", "Cancelled"
        REJECT = "REJECT", "Rejected"
        VOID = "VOID", "Voided"
        REVERSE = "REVERSE", "Reversed"
        EXPIRE = "EXPIRE", "Expired"
        REISSUE = "REISSUE", "Re-issued"

    payment = models.ForeignKey(PaymentInstrument, on_delete=models.CASCADE,
                                related_name="events")
    event = models.CharField(max_length=8, choices=Event.choices)
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12, blank=True)
    on = models.DateField(help_text="The business date of the event.")
    user = models.ForeignKey("auth.User", null=True, blank=True,
                             on_delete=models.SET_NULL)
    reference = models.CharField(max_length=120, blank=True)
    comment = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.payment_id} {self.event} {self.on}"


class PaymentAttachment(models.Model):
    payment = models.ForeignKey(PaymentInstrument, on_delete=models.CASCADE,
                                related_name="attachments")
    file = models.FileField(upload_to="payments/", null=True, blank=True)
    label = models.CharField(max_length=120, blank=True)
    text = models.TextField(blank=True)
    uploaded_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)
