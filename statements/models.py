from django.db import models


class BankAccount(models.Model):
    """A bank account the church holds. Statements and bank transactions can be
    tagged to an account so each can be reconciled separately. One account is the
    default, used automatically so existing single-account imports are unaffected."""
    class Kind(models.TextChoices):
        CURRENT = "CURRENT", "Current / operating"
        DEVELOPMENT = "DEVELOPMENT", "Development fund"
        SAVINGS = "SAVINGS", "Savings"
        OTHER = "OTHER", "Other"

    name = models.CharField(max_length=80)
    bank_name = models.CharField(max_length=80, blank=True)
    account_number = models.CharField(max_length=40, blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.CURRENT)
    is_default = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    @property
    def masked_number(self):
        n = (self.account_number or "").strip()
        return ("•••• " + n[-4:]) if len(n) >= 4 else n

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            BankAccount.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)

    @classmethod
    def get_default(cls):
        return (cls.objects.filter(is_default=True, active=True).first()
                or cls.objects.filter(active=True).first())


class StatementImport(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"
        PURGED = "PURGED", "Purged"

    uploaded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    bank_account = models.ForeignKey("statements.BankAccount", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="imports")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=255)
    file = models.FileField(upload_to="statements/", null=True, blank=True)
    total_rows = models.IntegerField(default=0)
    imported = models.IntegerField(default=0)
    duplicates_skipped = models.IntegerField(default=0)
    queued_for_review = models.IntegerField(default=0)
    failed = models.IntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    error_detail = models.TextField(blank=True)

    # Running-balance integrity check (uses the statement's own balance column as
    # a checksum). balance_check: "" (not run), "OK", "BROKEN", or "NO_BALANCE".
    balance_check = models.CharField(max_length=12, blank=True, default="")
    balance_detail = models.TextField(blank=True)

    # The statement's own opening/closing running balance and the date range it
    # covers. Persisted so a reconciliation report can compare the statement's
    # closing balance against the system's computed bank position — catching
    # entries that appear on the statement but never made it into the app.
    stmt_opening_balance = models.DecimalField(max_digits=14, decimal_places=2,
                                               null=True, blank=True)
    stmt_closing_balance = models.DecimalField(max_digits=14, decimal_places=2,
                                               null=True, blank=True)
    stmt_first_date = models.DateField(null=True, blank=True)
    stmt_last_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.filename} ({self.get_status_display()})"

    @property
    def progress_pct(self):
        if not self.total_rows:
            return 0
        done = self.imported + self.duplicates_skipped + self.queued_for_review + self.failed
        return min(100, round(done * 100 / self.total_rows))


class BankReconciliation(models.Model):
    """A bank reconciliation worksheet: start from the bank statement balance,
    add/subtract reconciling items (unpresented cheques, cash at hand, deposits
    in transit, bank charges…), and compare the adjusted balance to the cash-book
    balance."""
    statement_date = models.DateField(db_index=True)
    bank_balance = models.DecimalField(
        max_digits=14, decimal_places=2,
        help_text="Closing balance as per the bank statement.")
    book_balance = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Cash-book / ledger balance to reconcile against (optional).")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                   related_name="reconciliations")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-statement_date"]

    def __str__(self):
        return f"Reconciliation {self.statement_date}"

    @property
    def adjustments(self):
        from decimal import Decimal
        total = Decimal(0)
        for it in self.items.all():
            total += it.amount if it.effect == ReconciliationItem.Effect.ADD else -it.amount
        return total

    @property
    def can_delete(self):
        """A reconciliation may be removed within a week of being created — a
        guard against deleting settled historical worksheets."""
        from django.utils import timezone
        import datetime as _dt
        if not self.created_at:
            return True
        return timezone.now() - self.created_at <= _dt.timedelta(days=7)

    @property
    def adjusted_balance(self):
        return self.bank_balance + self.adjustments

    @property
    def difference(self):
        if self.book_balance is None:
            return None
        return self.adjusted_balance - self.book_balance

    @property
    def is_reconciled(self):
        d = self.difference
        return d is not None and d == 0


class ReconciliationItem(models.Model):
    class Kind(models.TextChoices):
        UNPRESENTED = "UNPRESENTED", "Unpresented cheque / payment"
        IN_TRANSIT = "IN_TRANSIT", "Deposit in transit"
        CASH_AT_HAND = "CASH_AT_HAND", "Cash at hand (not yet banked)"
        BANK_CHARGE = "BANK_CHARGE", "Bank charge not in books"
        INTEREST = "INTEREST", "Bank interest not in books"
        ERROR = "ERROR", "Error / correction"
        OTHER = "OTHER", "Other"

    class Effect(models.TextChoices):
        ADD = "ADD", "Add to bank balance"
        SUBTRACT = "SUBTRACT", "Subtract from bank balance"

    reconciliation = models.ForeignKey(BankReconciliation, on_delete=models.CASCADE,
                                       related_name="items")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.OTHER)
    description = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    effect = models.CharField(max_length=8, choices=Effect.choices, default=Effect.SUBTRACT)

    # sensible default direction per kind
    DEFAULT_EFFECT = {
        "UNPRESENTED": "SUBTRACT", "BANK_CHARGE": "SUBTRACT",
        "IN_TRANSIT": "ADD", "CASH_AT_HAND": "ADD", "INTEREST": "ADD",
    }

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount}"


class ReconciliationMatch(models.Model):
    """An auto-reconciliation suggestion: links a bank statement line (a DEBIT
    Transaction) to the internal Expense it most likely pays, with a confidence
    score and a human-readable reason."""
    class Status(models.TextChoices):
        AUTO = "AUTO", "Auto-matched"          # high confidence, linked
        REVIEW = "REVIEW", "Review required"   # medium confidence, needs confirmation
        CONFIRMED = "CONFIRMED", "Confirmed"
        REJECTED = "REJECTED", "Rejected"

    transaction = models.ForeignKey("giving.Transaction", on_delete=models.CASCADE,
                                    related_name="recon_matches")
    expense = models.ForeignKey("cashbook.Expense", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="recon_matches")
    confidence = models.PositiveSmallIntegerField(default=0)   # 0..100
    reason = models.CharField(max_length=240, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REVIEW)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_by = models.ForeignKey("auth.User", null=True, blank=True,
                                     on_delete=models.SET_NULL)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-confidence", "-created_at"]

    def __str__(self):
        return f"{self.transaction_id}→{self.expense_id} ({self.confidence}%)"


class BankEvent(models.Model):
    """Audit log of a single real-time transaction notification pushed by the
    bank's Core Banking System. Provides idempotency (the bank re-delivers on any
    non-2XX reply) and a record for troubleshooting/replay."""
    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSED = "PROCESSED", "Processed"
        DUPLICATE = "DUPLICATE", "Duplicate (ignored)"
        REJECTED = "REJECTED", "Rejected (bad/auth)"
        FAILED = "FAILED", "Failed to process"

    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    cbs_transaction_id = models.CharField(max_length=80, unique=True,
                                      help_text="The bank's unique TransactionId.")
    acct_no = models.CharField(max_length=40, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    event_type = models.CharField(max_length=10, blank=True)   # DEBIT / CREDIT
    currency = models.CharField(max_length=8, blank=True)
    payment_ref = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.RECEIVED, db_index=True)
    transaction = models.ForeignKey("giving.Transaction", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="bank_event")
    payload = models.TextField(blank=True)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.cbs_transaction_id} {self.event_type} {self.amount} [{self.status}]"
