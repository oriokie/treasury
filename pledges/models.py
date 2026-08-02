"""
Pledge management — informational commitment tracking.

CARDINAL RULE: a pledge is a *promise* to give, not income. Nothing in this
module ever posts to the general ledger or changes a fund balance. Fund balances
move only when a real giving.Transaction is recorded — exactly as before. A
Pledge is fulfilled by *matching* existing confirmed contributions to it
(PledgePayment), never by creating money. This keeps the books on a clean cash
basis and leaves every accounting invariant untouched, while giving leadership
full visibility of commitments vs receipts.
"""
import datetime as dt
from decimal import Decimal
from django.core.validators import MinValueValidator

from django.db import models
from django.db.models import Sum
from simple_history.models import HistoricalRecords


def _add_months(d, n):
    """Add n calendar months to a date, clamping the day to the month length."""
    import calendar
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return dt.date(y, m, day)


class PledgeCampaign(models.Model):
    """A giving drive members pledge toward (e.g. a building fund appeal). The
    target fund is informational here — money still lands in that fund through
    the normal contribution flow; the campaign only groups pledges and measures
    progress."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CLOSED = "CLOSED", "Closed"
        DRAFT = "DRAFT", "Draft"

    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    target_department = models.ForeignKey(
        "departments.Department", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="pledge_campaigns",
        help_text="The fund contributions toward this campaign land in. "
                  "Informational — money still flows through the normal ledger.")
    goal_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        help_text="Overall fundraising goal (optional).")
    start_date = models.DateField(default=dt.date.today)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    created_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-start_date", "name"]

    def __str__(self):
        return self.name

    @property
    def total_pledged(self):
        return (self.pledges.filter(status__in=[Pledge.Status.ACTIVE,
                Pledge.Status.FULFILLED, Pledge.Status.LAPSED])
                .aggregate(s=Sum("amount"))["s"] or Decimal("0"))

    @property
    def total_received(self):
        return (PledgePayment.objects.filter(pledge__campaign=self)
                .aggregate(s=Sum("amount"))["s"] or Decimal("0"))

    @property
    def percent_pledged(self):
        """Pledged as a share of the goal, capped at 100 for a progress bar.

        The pledge form has shown a progress bar bound to this property since
        3.44.0 — but the property did not exist, so the bar sat at 0% for every
        campaign (the template's |default:0 swallowed the error). Bars cap at
        100; the report states the real percentage as a figure where
        over-subscription is worth seeing.
        """
        if not self.goal_amount:
            return 0
        return int(min(self.total_pledged * 100 / self.goal_amount, 100))

    @property
    def total_outstanding(self):
        out = self.total_pledged - self.total_received
        return out if out > 0 else Decimal("0")

    @property
    def pct_received(self):
        base = self.total_pledged or self.goal_amount
        if not base:
            return 0
        return min(round(self.total_received / base * 100), 100)

    @property
    def pct_to_goal(self):
        if not self.goal_amount:
            return None
        return min(round(self.total_received / self.goal_amount * 100), 100)

    @property
    def pledge_count(self):
        return self.pledges.exclude(status=Pledge.Status.CANCELLED).count()


class Pledge(models.Model):
    """One member's promise within a campaign. The amount is a commitment, never
    income. Fulfilment is tracked by matching real contributions (PledgePayment)."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft (awaiting approval)"
        ACTIVE = "ACTIVE", "Active"
        FULFILLED = "FULFILLED", "Fulfilled"
        LAPSED = "LAPSED", "Lapsed"
        CANCELLED = "CANCELLED", "Cancelled"

    class Frequency(models.TextChoices):
        ONE_OFF = "ONE_OFF", "One-off"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"
        QUARTERLY = "QUARTERLY", "Quarterly"
        ANNUAL = "ANNUAL", "Annual"

    campaign = models.ForeignKey(PledgeCampaign, on_delete=models.PROTECT,
                                 related_name="pledges")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT,
                               related_name="pledges")
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        help_text="Total amount promised.")
    frequency = models.CharField(max_length=10, choices=Frequency.choices,
                                 default=Frequency.ONE_OFF)
    installment_amount = models.DecimalField(max_digits=12, decimal_places=2,
        null=True, blank=True,
        help_text="Per-installment amount for recurring pledges (optional).")
    start_date = models.DateField(default=dt.date.today)
    end_date = models.DateField(null=True, blank=True,
        help_text="When the pledge should be fully paid. Past this, an unpaid "
                  "pledge is treated as lapsed.")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.DRAFT, db_index=True)
    note = models.CharField(max_length=200, blank=True)

    recorded_by = models.ForeignKey("auth.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="pledges_recorded")
    approved_by = models.ForeignKey("auth.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="pledges_approved")
    approved_at = models.DateTimeField(null=True, blank=True)

    reminders_opt_out = models.BooleanField(default=False)

    # public self-submission (when the optional member pledge form is enabled).
    # A self-submitted pledge is held UNVERIFIED until a treasurer reviews it.
    self_submitted = models.BooleanField(default=False,
        help_text="Submitted by the member via the public pledge link.")
    submitted_contact = models.CharField(max_length=120, blank=True,
        help_text="Name/phone the member entered, for verification.")

    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    #: How long a leader may correct their own entry. A pledge is a promise
    #: someone made, and the record of it is not the leader's to revise once
    #: the church has acted on it — but a name mistyped at the desk should not
    #: need a treasurer either. A day covers the mistake and not the rewrite.
    LEADER_EDIT_WINDOW = dt.timedelta(days=1)

    def leader_editable(self, now=None):
        """Whether a leader may still change or withdraw this pledge.

        Only their own recent entries, and only while nothing has been paid
        against them: money received against a pledge makes it part of the
        church's records rather than the leader's draft.
        """
        from django.utils import timezone
        if self.paid:
            return False
        if not self.created_at:
            return True
        now = now or timezone.now()
        return (now - self.created_at) <= self.LEADER_EDIT_WINDOW

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [models.Index(fields=["status"]),
                   models.Index(fields=["campaign", "status"])]

    def __str__(self):
        return f"{self.member.name} -> {self.campaign.name}: {self.amount}"

    @property
    def paid(self):
        return (self.payments.aggregate(s=Sum("amount"))["s"] or Decimal("0"))

    @property
    def outstanding(self):
        out = self.amount - self.paid
        return out if out > 0 else Decimal("0")

    @property
    def pct_paid(self):
        if not self.amount:
            return 0
        return min(round(self.paid / self.amount * 100), 100)

    @property
    def is_fully_paid(self):
        return self.paid >= self.amount and self.amount > 0

    @property
    def is_overdue(self):
        return bool(self.end_date and self.end_date < dt.date.today()
                    and self.outstanding > 0
                    and self.status in (self.Status.ACTIVE, self.Status.LAPSED))

    def recompute_status(self, save=True):
        """Keep the lifecycle status honest against payments and dates. Never
        downgrades an explicit CANCELLED/DRAFT; only moves ACTIVE/FULFILLED/LAPSED."""
        if self.status in (self.Status.CANCELLED, self.Status.DRAFT):
            return self.status
        if self.is_fully_paid:
            new = self.Status.FULFILLED
        elif self.end_date and self.end_date < dt.date.today() and self.outstanding > 0:
            new = self.Status.LAPSED
        else:
            new = self.Status.ACTIVE
        if new != self.status:
            self.status = new
            if save:
                self.save(update_fields=["status"])
        return new

    def expected_installments(self):
        """The schedule of expected installments between start and end for
        recurring pledges. Returns a list of (due_date, amount). Informational."""
        if self.frequency == self.Frequency.ONE_OFF or not self.end_date:
            return [(self.end_date or self.start_date, self.amount)]
        dates = []
        d = self.start_date
        guard = 0
        while d <= self.end_date and guard < 520:
            dates.append(d)
            guard += 1
            if self.frequency == self.Frequency.WEEKLY:
                d = d + dt.timedelta(weeks=1)
            elif self.frequency == self.Frequency.MONTHLY:
                d = _add_months(d, 1)
            elif self.frequency == self.Frequency.QUARTERLY:
                d = _add_months(d, 3)
            elif self.frequency == self.Frequency.ANNUAL:
                d = _add_months(d, 12)
            else:
                break
        if not dates:
            dates = [self.start_date]
        per = self.installment_amount or (self.amount / len(dates)).quantize(Decimal("0.01"))
        out, running = [], Decimal("0")
        for i, dd in enumerate(dates):
            if i == len(dates) - 1:
                out.append((dd, self.amount - running))
            else:
                out.append((dd, per))
                running += per
        return out


class PledgePayment(models.Model):
    """Links a real, confirmed contribution to a pledge -- the *only* way a pledge
    is fulfilled. It records that money already in the ledger should be counted
    toward the promise. It carries no money of its own and never posts anywhere."""

    class Source(models.TextChoices):
        AUTO = "AUTO", "Auto-matched"
        MANUAL = "MANUAL", "Manually matched"

    pledge = models.ForeignKey(Pledge, on_delete=models.CASCADE,
                               related_name="payments")
    transaction = models.ForeignKey("giving.Transaction", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="pledge_payments",
        help_text="The confirmed contribution matched to this pledge.")
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    date = models.DateField(default=dt.date.today)
    source = models.CharField(max_length=6, choices=Source.choices,
                              default=Source.MANUAL)
    matched_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            # Unique per (pledge, transaction). No condition: MariaDB can't create
            # conditional constraints, and all of SQLite/MariaDB/Postgres treat NULL
            # as distinct in a unique index, so this still allows many manual
            # payments with no transaction while blocking the same contribution
            # being matched to a pledge twice.
            models.UniqueConstraint(fields=["pledge", "transaction"],
                                    name="uniq_pledge_transaction"),
        ]

    def __str__(self):
        return f"{self.amount} -> pledge {self.pledge_id}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.pledge.recompute_status()


class PledgeReminderLog(models.Model):
    """A record of a reminder sent to a member about a pledge -- so we don't spam,
    and leadership can see what was communicated. Reuses the SMS/WhatsApp layer."""

    class Channel(models.TextChoices):
        SMS = "SMS", "SMS"
        WHATSAPP = "WHATSAPP", "WhatsApp"

    pledge = models.ForeignKey(Pledge, on_delete=models.CASCADE,
                               related_name="reminders")
    channel = models.CharField(max_length=8, choices=Channel.choices)
    to = models.CharField(max_length=20)
    message = models.TextField()
    ok = models.BooleanField(default=False)
    sent_by = models.ForeignKey("auth.User", null=True, blank=True,
                                on_delete=models.SET_NULL)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.channel} to {self.to} ({self.sent_at:%Y-%m-%d})"


class PledgeMatchSuggestion(models.Model):
    """A system-flagged possible match between a confirmed contribution and an
    active pledge, awaiting a treasurer's confirm/dismiss. Created in SUGGEST mode
    so matching is never silently applied. Carries no money."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending review"
        CONFIRMED = "CONFIRMED", "Confirmed"
        DISMISSED = "DISMISSED", "Dismissed"

    transaction = models.ForeignKey("giving.Transaction", on_delete=models.CASCADE,
                                    related_name="pledge_suggestions")
    pledge = models.ForeignKey(Pledge, on_delete=models.CASCADE,
                               related_name="suggestions")
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["transaction", "pledge"],
                                    name="uniq_suggestion_txn_pledge"),
        ]

    def __str__(self):
        return f"suggest {self.amount} -> pledge {self.pledge_id}"
