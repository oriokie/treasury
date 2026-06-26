from decimal import Decimal
from django.db import models
from simple_history.models import HistoricalRecords


class Expense(models.Model):
    class Category(models.TextChoices):
        ALLOWANCE = "ALLOWANCE", "Allowance / honoraria"
        TRANSPORT = "TRANSPORT", "Transport"
        REFRESHMENTS = "REFRESHMENTS", "Refreshments / catering"
        MATERIALS = "MATERIALS", "Materials / supplies"
        STATIONERY = "STATIONERY", "Stationery / printing"
        UTILITIES = "UTILITIES", "Utilities (power, water)"
        MAINTENANCE = "MAINTENANCE", "Maintenance / repairs"
        CONSTRUCTION = "CONSTRUCTION", "Construction / development"
        EVANGELISM = "EVANGELISM", "Evangelism / mission"
        BENEVOLENCE = "BENEVOLENCE", "Benevolence / welfare"
        BANK_CHARGE = "BANK_CHARGE", "Bank charges"
        REMITTANCE = "REMITTANCE", "Remittance to field"
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

    date = models.DateField(db_index=True)
    sabbath_week = models.PositiveSmallIntegerField(null=True, blank=True)
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="expenses", db_index=True)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    category = models.CharField(max_length=14, choices=Category.choices,
                                default=Category.OTHER)
    expenditure_type = models.CharField(
        max_length=10, choices=ExpenditureType.choices,
        default=ExpenditureType.RECURRENT, db_index=True,
        help_text="Recurrent = day-to-day running cost; Capital = creates or "
                  "improves a fixed asset (construction, equipment, vehicles).")
    recurring = models.ForeignKey(
        "cashbook.RecurringExpense", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="generated", help_text="If set: the recurring schedule that created this expense.")
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
    claimant = models.CharField(max_length=120, blank=True)
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
                   models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.date} {self.description} {self.amount}"

    @property
    def category_display(self):
        return category_label(self.category)

    @property
    def affects_balance(self):
        return self.status in (self.Status.APPROVED, self.Status.PAID)


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
    amount = models.DecimalField(max_digits=12, decimal_places=2)
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
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    frequency = models.CharField(max_length=10, choices=Frequency.choices,
                                 default=Frequency.MONTHLY)
    day_of_month = models.PositiveSmallIntegerField(
        default=1, help_text="For monthly/quarterly/yearly schedules: day of the month the payment falls due.")
    claimant = models.CharField(max_length=120, blank=True)
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
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=200, blank=True)
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

    def __str__(self):
        return self.label or self.file.name


class Payable(models.Model):
    """An amount owed for goods/services received but not yet paid (a credit
    purchase). Tracked as an obligation; settling it records the actual payment."""
    date = models.DateField(db_index=True, help_text="Date the liability was incurred.")
    vendor = models.CharField(max_length=120)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="payables")
    category = models.CharField(max_length=14, choices=Expense.Category.choices,
                                default=Expense.Category.OTHER)
    due_date = models.DateField(null=True, blank=True)
    settled = models.BooleanField(default=False)
    settled_on = models.DateField(null=True, blank=True)
    settled_expense = models.OneToOneField(Expense, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="payable")
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="payables_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["settled", "due_date", "-date"]

    def __str__(self):
        return f"{self.vendor}: {self.amount}"


class Accrual(models.Model):
    """An expense incurred in a period but not yet invoiced/paid (e.g. an estimate
    for utilities consumed). A liability until settled."""
    date = models.DateField(db_index=True, help_text="Period-end the accrual relates to.")
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="accruals")
    category = models.CharField(max_length=14, choices=Expense.Category.choices,
                                default=Expense.Category.OTHER)
    settled = models.BooleanField(default=False)
    settled_on = models.DateField(null=True, blank=True)
    settled_expense = models.OneToOneField(Expense, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="accrual")
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
    amount = models.DecimalField(max_digits=12, decimal_places=2)
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
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date_issued = models.DateField()
    purpose = models.CharField(max_length=200)
    method = models.CharField(max_length=8, choices=Method.choices, default=Method.CASH)
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

    @property
    def balance(self):
        """Positive = surplus to recover from staff; negative = shortfall owed to staff."""
        return self.amount - self.settled_total


class ExpenseCategory(models.Model):
    """A church-defined expense category that supplements the built-in ones, so
    categories can be changed without code edits."""
    code = models.CharField(max_length=20, unique=True)
    label = models.CharField(max_length=80)
    active = models.BooleanField(default=True)
    sort = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ["sort", "label"]

    def __str__(self):
        return self.label


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
    amount = models.DecimalField(max_digits=12, decimal_places=2)
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
