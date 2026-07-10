"""Envelope giving.

A member hands in an envelope (cash, or marked as already given by bank) and the
treasurer records the split across funds against a sequential receipt number.

- CASH envelopes create one ENVELOPE-channel Transaction per fund line, so they
  flow into the central ledger and fund balances.
- BANK envelopes are linked to the existing bank Transaction (created at import);
  that transaction is marked `processed_via_envelope` so the same money is never
  counted twice. The envelope still records the split for the envelope reports.
"""
from django.db import models
from decimal import Decimal
from django.core.validators import MinValueValidator
from simple_history.models import HistoricalRecords


class Envelope(models.Model):
    class Channel(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK = "BANK", "Bank / M-Pesa"

    date = models.DateField(db_index=True, help_text="The Sabbath (Saturday) the envelope was given.")
    sabbath_week = models.PositiveSmallIntegerField(null=True, blank=True)
    receipt_no = models.CharField(max_length=20, unique=True, db_index=True)
    member = models.ForeignKey("members.Member", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="envelopes")
    contributor_name = models.CharField(
        max_length=120, help_text="As written on the envelope (may be a group or visitor).")
    channel = models.CharField(max_length=4, choices=Channel.choices, default=Channel.CASH)

    # for BANK envelopes: the statement row this envelope reconciles
    bank_transaction = models.OneToOneField(
        "giving.Transaction", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="envelope")

    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sms_sent = models.BooleanField(default=False)
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="envelopes_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "receipt_no"]
        indexes = [models.Index(fields=["date"])]

    def __str__(self):
        return f"#{self.receipt_no} {self.contributor_name} ({self.date})"

    def save(self, *args, **kwargs):
        # consistent uppercase register for contributor names
        if self.contributor_name:
            self.contributor_name = " ".join(self.contributor_name.upper().split())
        super().save(*args, **kwargs)

    def recompute_total(self):
        self.total = sum((l.amount for l in self.lines.all()), 0)
        return self.total

    @property
    def linked_transactions(self):
        """Ledger entries this envelope created (cash: per-line; bank: the deposit)."""
        txns = [l.transaction for l in self.lines.all() if l.transaction_id]
        if self.bank_transaction_id and self.bank_transaction not in txns:
            txns.append(self.bank_transaction)
        return [t for t in txns if t is not None]

    @property
    def is_voided(self):
        """True if every ledger entry behind this envelope has been reversed — so
        the receipt should be shown struck through in the list."""
        txns = self.linked_transactions
        return bool(txns) and all(t.is_reversed for t in txns)


class EnvelopeLine(models.Model):
    """One fund's share of an envelope."""
    envelope = models.ForeignKey(Envelope, on_delete=models.CASCADE, related_name="lines")
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    # optional development group this share belongs to (for Development giving),
    # so a single "Development" column can be split across groups without needing
    # a separate form column per group
    dev_group = models.ForeignKey("departments.DevelopmentGroup", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="envelope_lines")
    # the ledger row this line created (cash envelopes only)
    transaction = models.ForeignKey("giving.Transaction", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="envelope_lines")

    def __str__(self):
        return f"{self.department} {self.amount}"


class CountSession(models.Model):
    """A Sabbath cash-count record. Two or three counters independently verify the
    offering, record the denomination breakdown, and sign. The counted total is
    compared with the system's expected receipts to flag any discrepancy."""
    date = models.DateField(db_index=True, help_text="The Sabbath being counted.")
    counted_total = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        help_text="Sum of the denomination breakdown (cash counted).")
    expected_total = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        help_text="System receipts for the Sabbath, captured for comparison.")
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                                    related_name="count_sessions")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"Count {self.date}: {self.counted_total}"

    @property
    def discrepancy(self):
        return self.counted_total - self.expected_total

    @property
    def has_discrepancy(self):
        return abs(self.discrepancy) >= Decimal("0.01")


class CountDenomination(models.Model):
    """One denomination line of a count (e.g. 12 × KES 1000)."""
    session = models.ForeignKey(CountSession, on_delete=models.CASCADE,
                                related_name="denominations")
    denomination = models.DecimalField(max_digits=8, decimal_places=2)   # 1000, 500, 200, 100…
    count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-denomination"]

    @property
    def subtotal(self):
        return self.denomination * self.count


class CountWitness(models.Model):
    """A counter who verified and signed the count."""
    session = models.ForeignKey(CountSession, on_delete=models.CASCADE,
                                related_name="witnesses")
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=60, blank=True, help_text="e.g. Head deacon, Treasurer")
    signed = models.BooleanField(default=False)
