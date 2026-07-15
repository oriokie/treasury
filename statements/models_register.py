"""The Bank Statement Register — the bank's own record, kept separately.

A deliberately SEPARATE LAYER. Nothing in this module writes to the general
ledger, allocates money, creates a Transaction, or touches a fund balance. It
records what the BANK says happened, exactly as the bank said it, and then
answers one question: where do the bank's record and ours disagree?

Why separate at all, when `statements.StatementImport` already imports bank
files? Because that importer's job is to turn bank rows INTO ledger
transactions — it allocates, it matches members, it posts. A row it cannot
allocate goes to a review queue; a row it skips as a duplicate leaves no trace.
The register's job is the opposite and much simpler: keep every line the bank
ever sent, forever, unjudged, so that "what does the bank think our balance is"
and "what has the bank told us about that we have not recorded" are both
answerable without re-reading a spreadsheet.

The two are reconciled, not merged. A `StatementLine` here is matched to a
`giving.Transaction` by the bank's own unique identifiers — the M-Pesa receipt
or the core banking reference — because those are the only things both sides
genuinely agree on. Amount-and-date matching is deliberately NOT used: two
members giving the same amount on the same day is ordinary, and guessing there
would manufacture exactly the false reconciliation this exists to prevent.
"""
import datetime as _dt
from decimal import Decimal

from django.db import models

from simple_history.models import HistoricalRecords


class StatementRegisterImport(models.Model):
    """One upload of a bank statement file into the register.

    Kept distinct from `statements.StatementImport` (which posts to the ledger)
    even though both read the same file with the same parser: this one asserts
    nothing about the money, so it can be re-run over an overlapping period as
    often as a treasurer likes without any risk of double-posting. Re-importing
    January every month is a perfectly sensible thing to do, and here it is
    harmless — every line is deduplicated on the bank's own reference.
    """

    account = models.ForeignKey("statements.BankAccount", on_delete=models.PROTECT,
                                related_name="register_imports")
    uploaded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=255)
    rows_read = models.IntegerField(default=0)
    lines_added = models.IntegerField(default=0)
    duplicates_skipped = models.IntegerField(default=0)
    rows_failed = models.IntegerField(default=0)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    purged_at = models.DateTimeField(null=True, blank=True,
        help_text="When this upload was undone. A register import may be purged "
                  "on the DAY it was uploaded — a wrong file, the wrong account — "
                  "removing the lines it added. After that day it stays: the "
                  "register is additive and other work may rely on its lines.")
    purged_by = models.ForeignKey("auth.User", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.filename} ({self.lines_added} new lines)"

    @property
    def is_purged(self):
        return self.purged_at is not None

    @property
    def can_purge(self):
        """Only on the day of upload. The register is the bank's own record and
        purely additive, so undoing a same-day mis-upload is safe; after that a
        treasurer's reconciliation and exception work may reference its lines,
        and re-importing brings back anything genuinely still on the statement."""
        if self.is_purged:
            return False
        from django.utils import timezone
        return self.uploaded_at.date() == timezone.now().date()


class StatementLine(models.Model):
    """One line of a bank statement, exactly as the bank sent it.

    Never edited, never allocated, never posted. If the bank sent it, it is
    here; if the bank did not, it is not. That is the whole contract, and it is
    what makes this register usable as an independent check on our own books —
    a register a treasurer could quietly "correct" would be worth nothing.
    """

    account = models.ForeignKey("statements.BankAccount", on_delete=models.PROTECT,
                                related_name="register_lines")
    imported_in = models.ForeignKey(StatementRegisterImport, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="lines")

    date = models.DateField(db_index=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    credit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    debit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    # The bank's OWN running balance after this line, where the statement
    # carries one. Kept because it is the bank's assertion, and an assertion we
    # can check ours against — see `services.register.balance_drift()`.
    bank_balance = models.DecimalField(max_digits=14, decimal_places=2,
                                       null=True, blank=True)

    core_ref = models.CharField(max_length=64, blank=True, db_index=True)
    mpesa_ref = models.CharField(max_length=64, blank=True, db_index=True)
    receipt = models.CharField(max_length=64, blank=True, db_index=True)
    reference = models.CharField(max_length=120, blank=True)
    payer_name = models.CharField(max_length=160, blank=True)
    payer_phone = models.CharField(max_length=32, blank=True)
    raw_narration = models.TextField(blank=True)

    # The dedup key: whatever unique identifier the bank actually gave this
    # line. See `services.register.dedup_key()` for the precedence and why.
    dedup_key = models.CharField(max_length=80, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "occurred_at", "id"]
        constraints = [
            # The whole point of "should update the unique entries upon each
            # import": re-importing an overlapping period must add only what is
            # genuinely new. Enforced by the database, not by the importer's
            # good intentions.
            models.UniqueConstraint(fields=["account", "dedup_key"],
                                    name="uniq_register_line_per_account"),
        ]
        indexes = [
            models.Index(fields=["account", "date"]),
            models.Index(fields=["account", "dedup_key"]),
        ]

    def __str__(self):
        return f"{self.date} {self.signed_amount} {self.dedup_key}"

    @property
    def signed_amount(self):
        """Credit positive, debit negative — the direction the money moved as
        far as the BANK is concerned, which is the only direction this model
        has an opinion about."""
        return (self.credit or Decimal(0)) - (self.debit or Decimal(0))

    @property
    def direction(self):
        return "CREDIT" if (self.credit or 0) else "DEBIT"


class RegisterException(models.Model):
    """A disagreement between the bank's record and ours, found by
    `services.register.recheck()`.

    Two kinds, and they mean genuinely different things:

    * MISSING_IN_LEDGER — the bank says money moved, and we have no transaction
      for it. Real money the church has (or has lost) that its books do not
      know about. Usually an un-imported statement, sometimes a bank charge
      nobody recorded, occasionally something that needs asking about.

    * MISSING_IN_BANK — we have a bank-channel transaction the bank has never
      mentioned. Much rarer and much more serious: it means our books assert a
      bank movement the bank does not agree happened. Nearly always a
      hand-entered transaction that was miskeyed as BANK when it was cash, or
      entered twice.

    Stored rather than computed on the fly so an exception can be RESOLVED —
    with a reason, by a named person — and stay resolved. A discrepancy report
    that re-raises the same explained item every time it runs teaches a
    treasurer to ignore it, which is the opposite of what it is for.
    """

    class Kind(models.TextChoices):
        MISSING_IN_LEDGER = "MISSING_IN_LEDGER", "On the statement, not in our books"
        MISSING_IN_BANK = "MISSING_IN_BANK", "In our books, not on the statement"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RESOLVED = "RESOLVED", "Resolved"
        IGNORED = "IGNORED", "Accepted — no action needed"

    account = models.ForeignKey("statements.BankAccount", on_delete=models.CASCADE,
                                related_name="register_exceptions")
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.OPEN, db_index=True)

    # exactly one of these is set, depending on `kind`
    line = models.ForeignKey(StatementLine, null=True, blank=True,
                             on_delete=models.CASCADE, related_name="exceptions")
    transaction = models.ForeignKey("giving.Transaction", null=True, blank=True,
                                    on_delete=models.CASCADE,
                                    related_name="register_exceptions")

    # frozen at detection, so the report still reads correctly if the
    # underlying row is later deleted or edited
    date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    ref = models.CharField(max_length=80, blank=True)
    detail = models.CharField(max_length=255, blank=True)

    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    resolved_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=255, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            # No `condition` here, deliberately. MariaDB does not support
            # conditional unique constraints — it silently declines to create
            # them (Django warns W036) — so on Edwin's production database these
            # were not enforced at all, and a duplicate exception could be
            # written for the same line.
            #
            # The conditions were never needed. All three of SQLite, PostgreSQL
            # and MariaDB treat NULLs as DISTINCT in a unique index, so an
            # unconditional constraint permits any number of rows with
            # line=NULL (every MISSING_IN_BANK exception) while still enforcing
            # one row per (account, kind, line) where line IS set — which is
            # exactly what the condition was trying to express, and now actually
            # exists on every backend rather than only on the one nobody runs in
            # production.
            models.UniqueConstraint(fields=["account", "kind", "line"],
                                    name="uniq_exception_per_line"),
            models.UniqueConstraint(fields=["account", "kind", "transaction"],
                                    name="uniq_exception_per_txn"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.date} {self.amount}"

    @property
    def is_open(self):
        return self.status == self.Status.OPEN
