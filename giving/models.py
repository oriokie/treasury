from django.db import models
from decimal import Decimal, ROUND_HALF_UP
from django.core.validators import MinValueValidator
from simple_history.models import HistoricalRecords


class SplitFund(models.Model):
    """A collection a member gives as one lump sum that is split, by percentage,
    across several real funds — e.g. Combined Offering = 50% Trust + 50% Local.

    Giving to a split fund (by cash entry, envelope line, or a bank reference)
    is expanded into one ledger posting per component.
    """
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)[:50]
        super().save(*args, **kwargs)

    @property
    def percent_total(self):
        return sum((c.percent for c in self.components.all()), Decimal(0))

    def split(self, amount):
        """Return [(department, amount), …] summing exactly to `amount`.
        The last component absorbs any rounding remainder."""
        amount = Decimal(amount)
        comps = list(self.components.select_related("department").all())
        if not comps:
            return []
        out, running = [], Decimal(0)
        for i, c in enumerate(comps):
            if i == len(comps) - 1:
                share = amount - running
            else:
                share = (amount * c.percent / Decimal(100)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
                running += share
            out.append((c.department, share))
        return out


class SplitComponent(models.Model):
    split_fund = models.ForeignKey(SplitFund, on_delete=models.CASCADE,
                                   related_name="components")
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT)
    percent = models.DecimalField(max_digits=5, decimal_places=2,
                                  help_text="e.g. 50.00 for 50%.")

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.split_fund} → {self.department} {self.percent}%"


class AllocationRule(models.Model):
    """Maps a normalised payment reference to a department (or a split fund).
    SEED rules are trusted (AUTO); LEARNED rules come from the review queue."""

    class Source(models.TextChoices):
        SEED = "SEED", "Seeded"
        LEARNED = "LEARNED", "Learned"

    class MatchType(models.TextChoices):
        EXACT = "EXACT", "Matches exactly"
        STARTS = "STARTS", "Starts with"
        ENDS = "ENDS", "Ends with"
        CONTAINS = "CONTAINS", "Contains"

    match_type = models.CharField(max_length=8, choices=MatchType.choices,
                                  default=MatchType.EXACT)

    reference = models.CharField(max_length=60, db_index=True)  # normalised/lowercased
    valid_from = models.DateField(null=True, blank=True,
        help_text="First date this rule applies. Blank = no lower bound.")
    valid_to = models.DateField(null=True, blank=True,
        help_text="Last date this rule applies. Blank = no upper bound. "
                  "Leave both blank for a permanent rule (any period).")
    department = models.ForeignKey("departments.Department", on_delete=models.CASCADE,
                                   null=True, blank=True)
    split_fund = models.ForeignKey(SplitFund, on_delete=models.CASCADE,
                                   null=True, blank=True,
                                   help_text="If set, the reference splits across funds.")
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.LEARNED)
    history = HistoricalRecords()
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_period(self):
        return bool(self.valid_from or self.valid_to)

    def covers(self, date):
        """True if this rule applies on the given date (permanent rules always do)."""
        if not self.is_period:
            return True
        if date is None:
            return False
        if self.valid_from and date < self.valid_from:
            return False
        if self.valid_to and date > self.valid_to:
            return False
        return True

    class Meta:
        ordering = ["reference"]

    def __str__(self):
        target = self.split_fund or self.department
        return f"{self.reference} -> {target}"


class TransactionQuerySet(models.QuerySet):
    """One canonical definition of which rows count, so every report agrees."""
    def active(self):
        # exclude reversed originals and their contra entries (the 'live' set)
        return self.filter(is_reversed=False, is_reversal=False)

    def confirmed_credits(self):
        # recognised donations only: confirmed, not reversed, not a contra
        return self.active().filter(direction="CREDIT", confirmed=True)


class Transaction(models.Model):
    """The central ledger row. Every receipt and bank debit lives here."""

    class Channel(models.TextChoices):
        BANK = "BANK", "Bank / M-Pesa"
        CASH = "CASH", "Cash"
        ENVELOPE = "ENVELOPE", "Envelope"

    class Status(models.TextChoices):
        AUTO = "AUTO", "Auto-allocated"
        LEARNED = "LEARNED", "Learned rule"
        MANUAL = "MANUAL", "Manual"
        REVIEW = "REVIEW", "Needs review"

    class Direction(models.TextChoices):
        CREDIT = "CREDIT", "Credit (giving)"
        DEBIT = "DEBIT", "Debit (outflow)"

    date = models.DateField(db_index=True)
    sabbath_week = models.PositiveSmallIntegerField(null=True, blank=True)
    service_sabbath = models.DateField(null=True, blank=True, db_index=True,
        help_text="The Sabbath this gift is credited to (honours the count cutoff). "
                  "May differ from the transaction date for late/after-cutoff gifts.)")  # 1..5
    channel = models.CharField(max_length=8, choices=Channel.choices)
    direction = models.CharField(max_length=6, choices=Direction.choices,
                                 default=Direction.CREDIT)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Always positive; reversals create an explicit contra entry via reverse().")
    confirmed = models.BooleanField(default=True, db_index=True,
        help_text="Auto-allocated imports may be held unconfirmed until a treasurer "
                  "reviews them; unconfirmed rows do not affect balances or the ledger.")

    department = models.ForeignKey("departments.Department", null=True,
                                   on_delete=models.PROTECT, db_index=True)
    dev_group = models.ForeignKey("departments.DevelopmentGroup", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="transactions")
    member = models.ForeignKey("members.Member", null=True, blank=True,
                               on_delete=models.SET_NULL)

    reference = models.CharField(max_length=60, blank=True)
    payer_name = models.CharField(max_length=120, blank=True)
    payer_phone = models.CharField(max_length=12, blank=True)

    core_ref = models.CharField(max_length=40, unique=True, null=True, blank=True)
    bank_receipt = models.CharField(max_length=20, unique=True, null=True, blank=True)
    mpesa_ref = models.CharField(max_length=30, blank=True, db_index=True,
                                 help_text="M-Pesa / channel reference from the statement.")
    processed_via_envelope = models.BooleanField(
        default=False, db_index=True,
        help_text="A SYSTEM envelope record exists for this bank entry (it was "
                  "receipted through the app). Set by the receipt-bank-giving pull "
                  "and the per-gift receipt action. Kept out of the receipting flow "
                  "so it isn't receipted twice.")
    manual_receipt = models.BooleanField(
        default=False, db_index=True,
        help_text="This bank entry was receipted MANUALLY on paper (e.g. a "
                  "hand-written envelope) with no link to the ledger. No system "
                  "envelope is created. Kept out of both the review queue and the "
                  "receipt-bank-giving pull so it is never receipted again. "
                  "Reversible — untick to make it eligible for a system receipt.")
    sabbath_confirm_pending = models.BooleanField(
        default=False, db_index=True,
        help_text="Set by the statement importer when the gift's service Sabbath "
                  "had already passed on the day of import; the treasurer confirms "
                  "whether it stays on that Sabbath or moves to the next.")
    statement_import = models.ForeignKey("statements.StatementImport", null=True,
                                         blank=True, on_delete=models.SET_NULL,
                                         related_name="transactions")
    bank_account = models.ForeignKey("statements.BankAccount", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="transactions",
                                     help_text="Which bank account this entry belongs to.")

    allocation_status = models.CharField(max_length=8, choices=Status.choices,
                                         db_index=True)
    raw_narration = models.TextField(blank=True)

    claimed_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="claimed_txns")
    claimed_at = models.DateTimeField(null=True, blank=True)

    is_reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True, blank=True)
    is_reversal = models.BooleanField(default=False, help_text="A contra entry that reverses another.")
    excluded_from_income = models.BooleanField(default=False, db_index=True,
        help_text="A capital receipt (e.g. asset-disposal proceeds) — real cash in "
                  "the fund, but not operating income, so it is kept out of the "
                  "Income & Expenditure statement (only the gain/loss is shown there).")

    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()
    objects = TransactionQuerySet.as_manager()

    def save(self, *args, **kwargs):
        # keep names in a consistent uppercase register
        if self.payer_name:
            self.payer_name = " ".join(self.payer_name.upper().split())
        # default the service Sabbath: a gift for an already-closed Sabbath rolls
        # to the next open one, so a counted Sabbath is never reopened. The real
        # transaction date is never changed.
        if self.service_sabbath is None and self.date:
            import datetime as _dt
            d = self.date
            if isinstance(d, str):
                try:
                    d = _dt.date.fromisoformat(d)
                except ValueError:
                    d = None
            if isinstance(d, _dt.date):
                from core.models import service_sabbath_for
                self.service_sabbath = service_sabbath_for(d)
        super().save(*args, **kwargs)

    def reverse(self, user, reason=""):
        """Create a contra posting that nets this entry to zero, keeping both for
        the audit trail. Never deletes. Returns the contra Transaction."""
        from django.utils import timezone
        if self.is_reversed:
            raise ValueError("This entry has already been reversed.")
        if self.is_reversal:
            raise ValueError("A reversal entry cannot itself be reversed.")
        contra = Transaction.objects.create(
            date=self.date, sabbath_week=self.sabbath_week, channel=self.channel,
            direction=self.direction, amount=self.amount, department=self.department,
            dev_group=self.dev_group, member=self.member, reference=self.reference,
            payer_name=self.payer_name, payer_phone=self.payer_phone,
            confirmed=self.confirmed,
            allocation_status=Transaction.Status.MANUAL, is_reversal=True,
            raw_narration=f"[Reversal of #{self.pk}] {reason}".strip())
        self.is_reversed = True
        self.reversed_at = timezone.now()
        self.save(update_fields=["is_reversed", "reversed_at"])
        TransactionReversal.objects.create(
            original=self, contra=contra, reason=reason, created_by=user)
        return contra

    def split_siblings(self):
        """Other ledger rows that are parts of the SAME split gift as this one.

        A split offering (e.g. Combined Offering) is posted as several rows that
        share the payment reference, with the lump sum divided across funds. We
        group by the strongest shared identifier available: the bank core_ref
        base (rows X, X-S1, X-S2 …), else the M-Pesa reference, else the plain
        reference paired with the same date. Returns a queryset EXCLUDING self.
        """
        base = None
        if self.core_ref:
            base = self.core_ref.split("-S")[0]
        q = models.Q(pk__in=[])  # empty
        if base:
            q |= models.Q(core_ref=base) | models.Q(core_ref__startswith=f"{base}-S")
        if self.mpesa_ref:
            q |= models.Q(mpesa_ref__iexact=self.mpesa_ref, date=self.date)
        if self.reference:
            q |= models.Q(reference__iexact=self.reference, date=self.date)
        return Transaction.objects.filter(
            q, channel=self.channel, direction=self.direction,
            is_reversal=False, is_reversed=False).exclude(pk=self.pk)

    def mark_manual_receipt(self, value=True, cascade_split=True):
        """Mark this entry as receipted MANUALLY on paper: no system envelope is
        created, and it is kept out of BOTH the review queue and the
        receipt-bank-giving pull so it is never receipted again. A REVIEW credit
        is moved to MANUAL so it leaves the allocation queue.

        Reversible: pass value=False to un-mark it (making it eligible for a
        system receipt again). When `cascade_split` is on, every part of the same
        split gift is marked/unmarked together (so handling one half of a Combined
        Offering covers the whole gift). Returns the number of rows changed.
        """
        def _apply(t):
            changed = []
            if t.manual_receipt != value:
                t.manual_receipt = value
                changed.append("manual_receipt")
            if value:
                # being marked: also pull it out of the sabbath/review queues
                if t.sabbath_confirm_pending:
                    t.sabbath_confirm_pending = False
                    changed.append("sabbath_confirm_pending")
                if t.allocation_status == Transaction.Status.REVIEW:
                    t.allocation_status = Transaction.Status.MANUAL
                    changed.append("allocation_status")
            if changed:
                t.save(update_fields=changed)
                return True
            return False

        n = 1 if _apply(self) else 0
        if cascade_split:
            for sib in self.split_siblings():
                if _apply(sib):
                    n += 1
        return n

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [
            models.Index(fields=["date", "department"]),
            models.Index(fields=["allocation_status"]),
            models.Index(fields=["core_ref"]),
        ]

    def split_into(self, parts, user=None):
        """Split this single entry across several funds/groups. `parts` is a list
        of (department, amount, dev_group) tuples whose amounts must sum to this
        entry's amount. The original entry becomes the first part; the remaining
        parts are created as sibling entries. Used when one lump sum (e.g. a 2,000
        bank deposit) is meant for two funds or two development groups."""
        from decimal import Decimal
        clean = [(d, Decimal(str(a)), g) for (d, a, g) in parts
                 if a not in (None, "") and Decimal(str(a)) > 0]
        if len(clean) < 2:
            raise ValueError("Provide at least two parts to split into.")
        total = sum((a for _, a, _ in clean), Decimal(0))
        if total != self.amount:
            raise ValueError(
                f"The parts add up to {total:,.2f} but the entry is {self.amount:,.2f}. "
                "They must be equal.")
        if self.is_reversed or self.is_reversal:
            raise ValueError("A reversed entry cannot be split.")

        d0, a0, g0 = clean[0]
        self.department = d0
        self.dev_group = g0
        self.amount = a0
        self.allocation_status = Transaction.Status.MANUAL
        self.save()
        out = [self]
        for i, (d, a, g) in enumerate(clean[1:], start=1):
            out.append(Transaction.objects.create(
                date=self.date, sabbath_week=self.sabbath_week,
                service_sabbath=self.service_sabbath,
                sabbath_confirm_pending=self.sabbath_confirm_pending,
                channel=self.channel, direction=self.direction, amount=a,
                department=d, dev_group=g, member=self.member,
                reference=self.reference, payer_name=self.payer_name,
                payer_phone=self.payer_phone, confirmed=self.confirmed,
                mpesa_ref=self.mpesa_ref,
                statement_import=self.statement_import,
                bank_account_id=getattr(self, "bank_account_id", None),
                allocation_status=Transaction.Status.MANUAL,
                core_ref=(f"{self.core_ref}-S{i}" if self.core_ref else None),
                raw_narration=f"[Split of #{self.pk}] {self.raw_narration}"[:1000]))
        return out

    def __str__(self):
        return f"{self.date} {self.get_channel_display()} {self.amount}"

    @property
    def is_review(self):
        return self.allocation_status == self.Status.REVIEW


class TransactionReversal(models.Model):
    """Audit record of a transaction reversal (treasury never deletes — it reverses)."""
    original = models.OneToOneField(Transaction, on_delete=models.CASCADE,
                                    related_name="reversal_record")
    contra = models.OneToOneField(Transaction, on_delete=models.CASCADE,
                                   related_name="reverses")
    reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
