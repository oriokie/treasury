"""A classic double-entry layer behind the fund-accounting system.

A Chart of Accounts (the five elements: Asset, Liability, Equity, Income,
Expense) plus a journal of balanced entries derived from the source documents
(receipts, expenses, remittances and opening balances). This gives a trial
balance and a general ledger for audit, without changing how users record data.
"""
from decimal import Decimal
import datetime as _dt
from django.db import models


class Account(models.Model):
    class Type(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        EQUITY = "EQUITY", "Equity / fund balance"
        INCOME = "INCOME", "Income"
        EXPENSE = "EXPENSE", "Expense"

    DEBIT_NORMAL = {"ASSET", "EXPENSE"}

    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=10, choices=Type.choices, db_index=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="children")
    system_key = models.CharField(max_length=40, blank=True, db_index=True,
                                  help_text="Stable key used by the posting engine.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} · {self.name}"

    @property
    def is_debit_normal(self):
        return self.type in self.DEBIT_NORMAL


class JournalSequence(models.Model):
    """One row per year; tracks the last journal number issued that year. The
    counter only ever increases — a number is never reused, even if the entry
    it was assigned to is later deleted and replaced by a correction (the
    replacement gets its own new number; the original stays on record in
    JournalEntryArchive). This is what makes JV-2026-000001 a permanent
    reference an auditor can cite, not just a display convenience."""
    year = models.PositiveSmallIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    @classmethod
    def next_number(cls, year):
        from django.db import transaction
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(year=year)
            seq.last_number += 1
            seq.save(update_fields=["last_number"])
            return f"JV-{year}-{seq.last_number:06d}"


class JournalEntry(models.Model):
    """A balanced posting (sum of debits == sum of credits) derived from a source
    document. Stored so the ledger is auditable and queryable."""
    date = models.DateField(db_index=True)
    memo = models.CharField(max_length=200, blank=True)
    source_type = models.CharField(max_length=20, db_index=True)   # opening|transaction|expense|remittance
    source_id = models.IntegerField(null=True, blank=True, db_index=True)
    number = models.CharField(max_length=20, unique=True, null=True, blank=True,
        help_text="Permanent journal voucher reference (e.g. JV-2026-000001), "
                  "assigned once and never reused or renumbered — including when "
                  "this entry is later replaced by a correction (see "
                  "JournalEntryArchive, which preserves the original number).")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]
        indexes = [models.Index(fields=["source_type", "source_id"])]

    def __str__(self):
        return f"{self.number or ('#' + str(self.id))} {self.date} {self.memo}"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = JournalSequence.next_number((self.date or _dt.date.today()).year)
        super().save(*args, **kwargs)

    @property
    def total_debit(self):
        return sum((l.debit for l in self.lines.all()), Decimal(0))


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    department = models.ForeignKey("departments.Department", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="journal_lines",
                                   db_index=True)
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]


class JournalEntryArchive(models.Model):
    """A snapshot of a journal entry's detail taken just before it was deleted
    to be re-posted (e.g. a source document was edited after posting). The
    live ledger only ever holds the current, correct posting — deleting and
    recreating an entry is how every correction is applied — but that means a
    plain read of JournalEntry/JournalLine can never show what the ledger said
    *before* a correction. This table exists purely so that history is still
    available on request, without weakening the guarantee that the current
    ledger always reflects the current source documents.

    Controlled by SiteConfig.archive_replaced_ledger_entries — when off,
    entries are still replaced exactly as before, just without a snapshot."""
    date = models.DateField()
    memo = models.CharField(max_length=200, blank=True)
    source_type = models.CharField(max_length=20, db_index=True)
    source_id = models.IntegerField(null=True, blank=True, db_index=True)
    original_entry_id = models.IntegerField(db_index=True)
    original_number = models.CharField(max_length=20, blank=True, null=True,
        help_text="The permanent journal reference (e.g. JV-2026-000001) this "
                  "entry carried before it was replaced — preserved here since "
                  "that number is never reissued to anything else.")
    original_created_at = models.DateTimeField()
    lines = models.JSONField(
        help_text="[{account_code, account_name, department_id, debit, credit}, ...]")
    replaced_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-replaced_at"]
        indexes = [models.Index(fields=["source_type", "source_id"])]

    def __str__(self):
        return f"Replaced {self.date} {self.memo} (was entry #{self.original_entry_id})"

