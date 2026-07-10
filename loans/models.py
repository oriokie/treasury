"""Loan management.

A loan is a LIABILITY, never income. The module is deliberately thin on new
accounting machinery — every shilling still flows through the two existing
source-document types, so the general ledger, fund balances, bank
reconciliation and every report tie out with no new balance math:

  Loan receipt      a giving.Transaction on the financed fund with
                    excluded_from_income=True (cash in the fund, not income;
                    the same mechanism capital receipts already use). The
                    ledger posts it DR Cash / CR Loans payable — the exact
                    shape trust receipts already have (DR Cash / CR Trust
                    payable).
  Principal repaid  a cashbook.Expense with category=LOAN_REPAYMENT. The
                    ledger posts DR Loans payable / CR Cash and the Income &
                    Expenditure statement excludes it — the exact treatment
                    trust REMITTANCE expenses already receive (a liability
                    settlement, not expenditure).
  Interest paid     an ordinary cashbook.Expense (category=LOAN_INTEREST) —
                    a true expense, in I&E as normal.
  Conversion /      a PAIR of documents dated on the conversion day: an
  write-off         income Transaction (the gift) plus a LOAN_REPAYMENT
                    Expense (the liability settled), which net to zero cash
                    and post, combined, DR Loans payable / CR Income —
                    exactly the journal the requirement specifies, expressed
                    through existing primitives so /ledger/rebuild/
                    regenerates it with no loan-specific rebuild step.

LoanTransaction rows are the loan-side index over those documents; a loan's
outstanding balance is always COMPUTED from them (single source of truth),
never manually maintained.

Adjustments are deliberately not a free-standing transaction kind: a
recording error is corrected by editing or reversing the underlying source
document, which re-posts automatically — the same correction philosophy the
rest of the application follows.
"""
import datetime as _dt
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from simple_history.models import HistoricalRecords

from members.models import name_key, normalize_phone


class Lender(models.Model):
    """Whoever lent the church money. NOT assumed to be a member — may be a
    visitor, an institution, another church — but can be linked to a Member,
    after which future loans resolve to that member automatically."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    class Source(models.TextChoices):
        MANUAL = "MANUAL", "Entered manually"
        AUTO_BANK = "AUTO_BANK", "Created from bank statement"

    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120, db_index=True, editable=False)
    phone = models.CharField(max_length=12, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    national_id = models.CharField(max_length=20, blank=True, db_index=True)
    address = models.CharField(max_length=200, blank=True)
    member = models.ForeignKey("members.Member", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="lender_records",
                               help_text="The church member this lender is, if any. "
                                         "Never created automatically.")
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    source = models.CharField(max_length=12, choices=Source.choices,
                              default=Source.MANUAL)
    merged_into = models.ForeignKey("self", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="merged_from",
                                    help_text="If set: this duplicate was merged into that lender "
                                              "(kept for the audit trail, no longer used).")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["name_key"]), models.Index(fields=["phone"])]

    def save(self, *args, **kwargs):
        self.name_key = name_key(self.name)
        self.phone = normalize_phone(self.phone) or (self.phone or "")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    @property
    def active_loans(self):
        return self.loans.filter(status=Loan.Status.ACTIVE)

    @property
    def outstanding_total(self):
        return sum((l.outstanding_principal for l in
                    self.loans.exclude(status=Loan.Status.DRAFT)), Decimal(0))


class LoanSequence(models.Model):
    """One row per year; last loan number issued that year. Counter only ever
    increases — a number is never reused (same guarantee as JournalSequence),
    so LN-2026-0001 is a permanent reference."""
    year = models.PositiveSmallIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    @classmethod
    def next_number(cls, year):
        from django.db import transaction
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(year=year)
            seq.last_number += 1
            seq.save(update_fields=["last_number"])
            return f"LN-{year}-{seq.last_number:04d}"


class Loan(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"
        CONVERTED = "CONVERTED", "Converted to donation"
        WRITTEN_OFF = "WRITTEN_OFF", "Written off"

    class LoanType(models.TextChoices):
        INDIVIDUAL = "INDIVIDUAL", "Individual"
        ORGANIZATION = "ORGANIZATION", "Organization / institution"
        CHURCH = "CHURCH", "Another church"
        OTHER = "OTHER", "Other"

    class InterestMethod(models.TextChoices):
        NONE = "NONE", "Interest-free"
        SIMPLE = "SIMPLE", "Simple interest (p.a. on outstanding)"
        FLAT = "FLAT", "Flat (p.a. on agreed principal)"

    number = models.CharField(max_length=20, unique=True, editable=False,
        help_text="Permanent loan reference (e.g. LN-2026-0001); assigned once, never reused.")
    lender = models.ForeignKey(Lender, on_delete=models.PROTECT, related_name="loans")
    loan_type = models.CharField(max_length=12, choices=LoanType.choices,
                                 default=LoanType.INDIVIDUAL)
    fund = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                             related_name="loans",
                             help_text="The local fund this loan finances. Loan money "
                                       "raises this fund's available cash but never its income.")
    project = models.CharField(max_length=120, blank=True,
                               help_text="Optional project this loan finances.")
    purpose = models.CharField(max_length=200, blank=True)
    principal_amount = models.DecimalField(max_digits=12, decimal_places=2,
        null=True, blank=True, validators=[MinValueValidator(Decimal("0.01"))],
        help_text="The agreed principal (optional). Money actually received is "
                  "tracked by receipt transactions; the outstanding balance is "
                  "always computed from transactions, never from this figure.")
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0,
        help_text="Annual interest rate in percent. 0 for interest-free.")
    interest_method = models.CharField(max_length=8, choices=InterestMethod.choices,
                                       default=InterestMethod.NONE)
    loan_date = models.DateField(db_index=True)
    maturity_date = models.DateField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL,
                                   related_name="loans_created")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-loan_date", "-id"]
        indexes = [models.Index(fields=["status", "fund"]),
                   models.Index(fields=["maturity_date"])]

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = LoanSequence.next_number(
                (self.loan_date or _dt.date.today()).year)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.number} · {self.lender.name}"

    # ---- computed balances (single source of truth: the transactions) ----
    def _sum(self, kind, on=None):
        qs = self.transactions.filter(kind=kind)
        if on:
            qs = qs.filter(date__lte=on)
        return sum((t.amount for t in qs if t.effective), Decimal(0))

    @property
    def received_total(self):
        return self._sum(LoanTransaction.Kind.RECEIPT)

    @property
    def principal_repaid(self):
        return self._sum(LoanTransaction.Kind.PRINCIPAL)

    @property
    def converted_total(self):
        return self._sum(LoanTransaction.Kind.CONVERSION)

    @property
    def written_off_total(self):
        return self._sum(LoanTransaction.Kind.WRITE_OFF)

    @property
    def interest_paid(self):
        return self._sum(LoanTransaction.Kind.INTEREST)

    @property
    def outstanding_principal(self):
        return (self.received_total - self.principal_repaid
                - self.converted_total - self.written_off_total)

    def outstanding_asof(self, on):
        return (self._sum(LoanTransaction.Kind.RECEIPT, on)
                - self._sum(LoanTransaction.Kind.PRINCIPAL, on)
                - self._sum(LoanTransaction.Kind.CONVERSION, on)
                - self._sum(LoanTransaction.Kind.WRITE_OFF, on))

    def accrued_interest(self, as_of=None):
        """Indicative interest accrued to date (cash-basis app: shown for
        information; interest hits the books only when actually PAID, as a
        LOAN_INTEREST expense). SIMPLE accrues day-by-day on the outstanding
        principal; FLAT accrues on the agreed principal."""
        if self.interest_method == self.InterestMethod.NONE or not self.interest_rate:
            return Decimal(0)
        as_of = as_of or _dt.date.today()
        if as_of <= self.loan_date:
            return Decimal(0)
        rate = self.interest_rate / Decimal(100)
        if self.interest_method == self.InterestMethod.FLAT:
            base = self.principal_amount or self.received_total
            days = (as_of - self.loan_date).days
            return (base * rate * days / Decimal(365)).quantize(Decimal("0.01"))
        # SIMPLE: integrate over the outstanding balance between events
        events = sorted(
            [t for t in self.transactions.all()
             if t.effective and t.kind != LoanTransaction.Kind.INTEREST],
            key=lambda t: (t.date, t.pk))
        total = Decimal(0)
        bal = Decimal(0)
        prev = self.loan_date
        for t in events:
            d = min(t.date, as_of)
            if d > prev and bal > 0:
                total += bal * rate * (d - prev).days / Decimal(365)
            prev = max(prev, min(t.date, as_of))
            sign = 1 if t.kind == LoanTransaction.Kind.RECEIPT else -1
            bal += sign * t.amount
        if as_of > prev and bal > 0:
            total += bal * rate * (as_of - prev).days / Decimal(365)
        return total.quantize(Decimal("0.01"))

    @property
    def outstanding_interest(self):
        return max(Decimal(0), self.accrued_interest() - self.interest_paid)

    @property
    def is_overdue(self):
        return (self.status == self.Status.ACTIVE and self.maturity_date
                and self.maturity_date < _dt.date.today()
                and self.outstanding_principal > 0)

    @property
    def is_editable(self):
        """Completed / converted / written-off loans are read-only."""
        return self.status in (self.Status.DRAFT, self.Status.ACTIVE)

    def refresh_status(self, save=True):
        """Derive status from the transactions. Never demotes DRAFT (that is a
        deliberate 'not yet in force' state) and decides between COMPLETED /
        CONVERTED / WRITTEN_OFF by what retired the larger share of the loan."""
        if self.status == self.Status.DRAFT:
            return self.status
        out = self.outstanding_principal
        if out > 0 or self.received_total == 0:
            new = self.Status.ACTIVE
        else:
            conv, wo = self.converted_total, self.written_off_total
            if conv and conv >= wo and conv >= self.principal_repaid:
                new = self.Status.CONVERTED
            elif wo and wo > conv and wo >= self.principal_repaid:
                new = self.Status.WRITTEN_OFF
            else:
                new = self.Status.COMPLETED
        if new != self.status:
            self.status = new
            if save:
                self.save(update_fields=["status"])
        return self.status


class LoanTransaction(models.Model):
    """One loan event, indexing the source document(s) that carry its money.
    The documents are authoritative: this row is only 'effective' while its
    underlying document(s) still count (receipt confirmed and not reversed;
    expense approved/paid) — so reversing or rejecting a document flows
    straight through to the loan's computed balance."""

    class Kind(models.TextChoices):
        RECEIPT = "RECEIPT", "Loan receipt"
        PRINCIPAL = "PRINCIPAL", "Principal repayment"
        INTEREST = "INTEREST", "Interest payment"
        CONVERSION = "CONVERSION", "Converted to donation"
        WRITE_OFF = "WRITE_OFF", "Write off"

    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name="transactions")
    kind = models.CharField(max_length=10, choices=Kind.choices, db_index=True)
    date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 validators=[MinValueValidator(Decimal("0.01"))])
    # RECEIPT: the bank/cash credit on the financed fund (excluded from income)
    receipt_transaction = models.OneToOneField(
        "giving.Transaction", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="loan_receipt")
    # CONVERSION / WRITE_OFF: the income (gift) half of the contra pair
    income_transaction = models.OneToOneField(
        "giving.Transaction", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="loan_retirement_income")
    # PRINCIPAL / INTEREST / CONVERSION / WRITE_OFF: the settlement expense
    expense = models.OneToOneField(
        "cashbook.Expense", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="loan_transaction")
    # RECEIPT into petty cash: the top-up recording the cash entering the float
    # (the loan money physically landed in the petty box). Kept linked so it is
    # reversed with the receipt and never orphaned.
    petty_topup = models.OneToOneField(
        "cashbook.PettyCashTopUp", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="loan_transaction")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL,
                                   related_name="loan_transactions_created")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["date", "id"]
        indexes = [models.Index(fields=["loan", "kind"]), models.Index(fields=["date"])]

    def __str__(self):
        return f"{self.loan.number} {self.get_kind_display()} {self.amount}"

    @property
    def effective(self):
        """Whether the underlying source document(s) still count."""
        from cashbook.models import Expense
        if self.kind == self.Kind.RECEIPT:
            t = self.receipt_transaction
            return bool(t and t.confirmed and not t.is_reversed and not t.is_reversal)
        if self.kind in (self.Kind.PRINCIPAL, self.Kind.INTEREST):
            e = self.expense
            return bool(e and e.status in (Expense.Status.APPROVED, Expense.Status.PAID))
        # conversion / write-off: both halves must stand
        t, e = self.income_transaction, self.expense
        t_ok = bool(t and t.confirmed and not t.is_reversed and not t.is_reversal)
        e_ok = bool(e and e.status in (Expense.Status.APPROVED, Expense.Status.PAID))
        return t_ok and e_ok


class LoanNarrationPattern(models.Model):
    """A configurable, database-driven narration pattern that flags a bank
    reference as loan money, using the same normalisation and match-type
    semantics as the allocation engine's rules (giving.AllocationRule) — this
    is a companion to that engine, not another parser: the importer runs it
    on the same normalize_reference() output, before ordinary allocation."""

    class Kind(models.TextChoices):
        RECEIPT = "RECEIPT", "Loan receipt (money in)"
        REPAYMENT = "REPAYMENT", "Loan repayment (money out)"
        INTEREST = "INTEREST", "Loan interest (money out)"
        CONVERSION = "CONVERSION", "Loan converted to donation"

    class MatchType(models.TextChoices):     # mirrors AllocationRule.MatchType
        EXACT = "EXACT", "Matches exactly"
        STARTS = "STARTS", "Starts with"
        ENDS = "ENDS", "Ends with"
        CONTAINS = "CONTAINS", "Contains"
        REGEX = "REGEX", "Matches a pattern (regex)"

    pattern = models.CharField(max_length=60,
        help_text="Compared against the normalised reference (lowercased, no spaces).")
    match_type = models.CharField(max_length=8, choices=MatchType.choices,
                                  default=MatchType.CONTAINS)
    kind = models.CharField(max_length=10, choices=Kind.choices,
                            default=Kind.RECEIPT, db_index=True)
    fund = models.ForeignKey("departments.Department", null=True, blank=True,
                             on_delete=models.CASCADE,
                             help_text="For receipts: the fund the loan finances. "
                                       "Blank = fund unknown, item goes to the review queue.")
    active = models.BooleanField(default=True, db_index=True)
    seeded = models.BooleanField(default=False, editable=False,
        help_text="Installed by the system (may be edited or deactivated freely).")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["kind", "-active", "pattern"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.pattern} ({self.get_match_type_display()})"

    def matches(self, normalized):
        import re
        p = (self.pattern or "").strip().lower().replace(" ", "")
        if not p or not normalized:
            return False
        if self.match_type == self.MatchType.REGEX:
            try:
                return bool(re.search(self.pattern, normalized))
            except re.error:
                return False       # a malformed pattern never matches (and never crashes)
        return ((self.match_type == self.MatchType.EXACT and normalized == p)
                or (self.match_type == self.MatchType.STARTS and normalized.startswith(p))
                or (self.match_type == self.MatchType.ENDS and normalized.endswith(p))
                or (self.match_type == self.MatchType.CONTAINS and p in normalized))


def loan_attachment_path(instance, filename):
    return f"loans/{instance.loan_id}/{filename}"


class LoanAttachment(models.Model):
    """Supporting documents for a loan (agreement, board minute, ID copy)."""
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=loan_attachment_path)
    label = models.CharField(max_length=120, blank=True)
    uploaded_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.label or self.file.name
