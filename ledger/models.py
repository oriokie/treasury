"""A classic double-entry layer behind the fund-accounting system.

A Chart of Accounts (the five elements: Asset, Liability, Equity, Income,
Expense) plus a journal of balanced entries derived from the source documents
(receipts, expenses, remittances and opening balances). This gives a trial
balance and a general ledger for audit, without changing how users record data.
"""
from decimal import Decimal
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


class JournalEntry(models.Model):
    """A balanced posting (sum of debits == sum of credits) derived from a source
    document. Stored so the ledger is auditable and queryable."""
    date = models.DateField(db_index=True)
    memo = models.CharField(max_length=200, blank=True)
    source_type = models.CharField(max_length=20, db_index=True)   # opening|transaction|expense|remittance
    source_id = models.IntegerField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]
        indexes = [models.Index(fields=["source_type", "source_id"])]

    def __str__(self):
        return f"{self.date} {self.memo}"

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
