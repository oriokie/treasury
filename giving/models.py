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
        REGEX = "REGEX", "Matches a pattern (regex)"

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
    archived = models.BooleanField(default=False, db_index=True,
        help_text="Archived rules are kept for the audit trail but no longer "
                  "used to allocate new giving.")
    archived_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_period(self):
        return bool(self.valid_from or self.valid_to)

    @property
    def is_expired(self):
        """A temporary rule whose validity window has ended."""
        import datetime as _d
        return bool(self.valid_to and self.valid_to < _d.date.today())

    def archive(self):
        from django.utils import timezone
        if not self.archived:
            self.archived = True
            self.archived_at = timezone.now()
            self.save(update_fields=["archived", "archived_at"])

    def restore(self):
        if self.archived:
            self.archived = False
            self.archived_at = None
            self.save(update_fields=["archived", "archived_at"])

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


class DevGroupPattern(models.Model):
    """A configurable regex used to recognise development-group contributions in
    bank narrations (e.g. 'DEVGR7', 'dev grp 11', 'DEV GP39'). Patterns are
    matched against the normalised (lowercased, spaces removed) reference.

    - NUMBERED patterns must contain one capturing group for the group number;
      a match routes the gift to that development group.
    - WORD patterns just flag the reference as development (no number) so it is
      booked to the development fund and queued for a group to be assigned.

    Replaces the previously hard-coded regexes so treasurers can manage the
    spellings without a code change."""
    class Kind(models.TextChoices):
        NUMBERED = "NUMBERED", "Captures a group number"
        WORD = "WORD", "Development marker (no number)"

    label = models.CharField(max_length=60,
        help_text="A short name for this pattern, e.g. 'dev/grp + number'.")
    pattern = models.CharField(max_length=200,
        help_text="Python regex, matched against the normalised reference "
                  "(lowercase, no spaces). NUMBERED patterns need one (…) group "
                  "for the number.")
    kind = models.CharField(max_length=10, choices=Kind.choices,
                            default=Kind.NUMBERED)
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100,
        help_text="Lower numbers are tried first.")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.label

    def clean(self):
        import re
        from django.core.exceptions import ValidationError
        try:
            rx = re.compile(self.pattern)
        except re.error as exc:
            raise ValidationError({"pattern": f"Invalid regular expression: {exc}"})
        if self.kind == self.Kind.NUMBERED and rx.groups < 1:
            raise ValidationError({"pattern": "A numbered pattern needs a capturing "
                "group '(\\d+)' for the group number."})


class TransactionQuerySet(models.QuerySet):
    """One canonical definition of which rows count, so every report agrees."""
    def active(self):
        # exclude reversed originals and their contra entries (the 'live' set)
        return self.filter(is_reversed=False, is_reversal=False)

    def confirmed_credits(self):
        # recognised donations only: confirmed, not reversed, not a contra
        return self.active().filter(direction="CREDIT", confirmed=True)

    def signed_cash_total(self):
        """Aggregate the queryset's true effect on cash, signed the ONE
        canonical way (see Transaction.signed_cash_case): reversals negative,
        debits negative, bank-memo rows zero. Every running balance, cash book
        and export total should use this (or the Case directly) rather than
        re-deriving the signs — re-derivation is exactly how the reversal and
        memo double-counts happened."""
        from decimal import Decimal
        from django.db.models import Sum
        agg = self.aggregate(total=Sum(Transaction.signed_cash_case()))
        return agg["total"] or Decimal(0)


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
        help_text="The Sabbath this contribution is credited to (honours the count cutoff). "
                  "May differ from the transaction date for late/after-cutoff contributions.)")  # 1..5
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
                  "and the per-contribution receipt action. Kept out of the receipting flow "
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
        help_text="Set by the statement importer when the contribution's service Sabbath "
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

    # Campaign fallback allocation (e.g. camp-meeting expense contributions
    # matched to a group when the normal rules miss). Kept on the row so the
    # group is reportable; cleared (SET_NULL) if the campaign is later deleted.
    campaign = models.ForeignKey("giving.Campaign", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="transactions")
    campaign_group = models.CharField(max_length=40, blank=True)

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

    def strict_split_siblings(self):
        """Like split_siblings(), but deliberately narrower: only matches by
        a bank-assigned unique identifier (the core_ref base, or an exact
        M-Pesa reference) — never the loose "same reference text + date"
        fallback split_siblings() also checks.

        That fallback exists because a CASH entry has no bank identifier at
        all, so cash-side split cascades (e.g. deleting a cash entry and its
        siblings) genuinely need it. But a plain reference like "tithe" or
        "offering" is payer-entered free text, not a unique identifier —
        two completely unrelated people can easily enter the same one on
        the same day, and reference-based matching would wrongly treat them
        as parts of the same split. Used by "send back to review", which
        must never combine two different people's unrelated gifts into one
        entry just because they typed the same word."""
        base = None
        if self.core_ref:
            base = self.core_ref.split("-S")[0]
        q = models.Q(pk__in=[])
        if base:
            q |= models.Q(core_ref=base) | models.Q(core_ref__startswith=f"{base}-S")
        if self.mpesa_ref:
            q |= models.Q(mpesa_ref__iexact=self.mpesa_ref, date=self.date)
        if not base and not self.mpesa_ref:
            return Transaction.objects.none()
        return Transaction.objects.filter(
            q, channel=self.channel, direction=self.direction,
            is_reversal=False, is_reversed=False).exclude(pk=self.pk)

    def split_siblings(self):
        """Other ledger rows that are parts of the SAME split contribution as this one.

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

    @property
    def is_bank_memo(self):
        """True when this BANK row is the memo half of a manually-receipted
        pair: the same money was (or will be) entered as an ENVELOPE
        transaction carrying the income and fund, and ``mark_manual_receipt``
        turned this bank line into a memo (excluded from income, detached from
        its fund). Its cash lives on the envelope row, so in any cash
        aggregation this row must contribute ZERO — counting both halves is
        the double-count. NOTE: ``processed_via_envelope`` rows are NOT memos —
        that flow attaches an envelope record to this bank row without a second
        ledger posting, so the bank row is still the money."""
        return self.channel == self.Channel.BANK and self.manual_receipt

    @property
    def signed_cash_amount(self):
        """This row's true effect on cash: zero for a bank-memo row (its cash
        lives on the envelope counterpart), negative for a reversal (an
        offsetting entry, stored with the original's direction and a positive
        amount by design) and for a debit, otherwise the amount. The
        per-instance twin of ``signed_cash_case`` — keep the two in step."""
        from decimal import Decimal
        if self.is_bank_memo:
            return Decimal(0)
        if self.is_reversal or self.direction == self.Direction.DEBIT:
            return -self.amount
        return self.amount

    @staticmethod
    def signed_cash_case():
        """The SQL twin of ``signed_cash_amount`` for aggregates: a Case
        expression signing each row by its true effect on cash. Defined once so
        the transactions page, exports and cash book can never disagree about
        what a row does to the balance."""
        from django.db.models import (Case, DecimalField, F, Value, When)
        return Case(
            When(channel=Transaction.Channel.BANK, manual_receipt=True,
                 then=Value(0)),
            When(is_reversal=True, then=-F("amount")),
            When(direction=Transaction.Direction.DEBIT, then=-F("amount")),
            default=F("amount"),
            output_field=DecimalField(max_digits=14, decimal_places=2))

    def mark_manual_receipt(self, value=True, cascade_split=True):
        """Mark this entry as receipted MANUALLY on paper: no system envelope is
        created, and it is kept out of BOTH the review queue and the
        receipt-bank-giving pull so it is never receipted again. A REVIEW credit
        is moved to MANUAL so it leaves the allocation queue.

        Reversible: pass value=False to un-mark it (making it eligible for a
        system receipt again). When `cascade_split` is on, every part of the same
        split contribution is marked/unmarked together (so handling one half of a Combined
        Offering covers the whole contribution). Returns the number of rows changed.
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
            # A receipted BANK credit is the same money as an envelope (which is
            # the income), so it becomes a memo — excluded from income and detached
            # from its fund — so it neither counts as income nor inflates the fund
            # balance. Un-marking re-includes it (it returns to allocation).
            if t.channel == Transaction.Channel.BANK:
                if value:
                    if not t.excluded_from_income:
                        t.excluded_from_income = True
                        changed.append("excluded_from_income")
                    if t.department_id is not None:
                        t.department = None
                        changed.append("department")
                else:
                    if t.excluded_from_income:
                        t.excluded_from_income = False
                        changed.append("excluded_from_income")
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
            # confirmed_credits().filter(date__range=...) — the shape behind
            # almost every collections/income report and dashboard KPI
            models.Index(fields=["direction", "confirmed", "date"],
                        name="giving_txn_dir_conf_date_idx"),
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

        from django.db import transaction as _db_transaction
        with _db_transaction.atomic():
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


class Campaign(models.Model):
    """A time-boxed appeal (e.g. Camp Meeting) whose contributions are allocated
    to one department and tagged to a member's group. Used as a FALLBACK after
    the normal allocation rules miss: a credit whose reference contains one of
    the campaign's trigger strings is matched to a campaign member (by phone or
    a unique name) and allocated to the campaign's department. Delete the whole
    campaign when the appeal ends — its member table goes with it and the rows it
    allocated keep their group tag for the record.
    """
    name = models.CharField(max_length=80, unique=True)
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT,
                                   related_name="campaigns",
                                   help_text="Fund these contributions are allocated to.")
    triggers = models.TextField(blank=True,
        help_text="Words that mark a reference as belonging to this campaign — "
                  "comma- or line-separated (e.g. expense, campexpense). The "
                  "fallback only fires when the reference contains one of these.")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def trigger_list(self):
        import re
        return [t.strip().lower() for t in re.split(r"[,\n;]", self.triggers or "")
                if t.strip()]

    def match_member(self, name, phone):
        """Phone first (if it identifies exactly one member), else a unique name."""
        from members.models import name_key, normalize_phone
        ph = normalize_phone(phone)
        if ph:
            qs = self.members.filter(phone=ph)
            if qs.count() == 1:
                return qs.first()
        key = name_key(name)
        if key:
            qs = self.members.filter(name_key=key)
            if qs.count() == 1:
                return qs.first()
        return None

    def subgroup_department(self, group_name):
        """The fund a matched member's contribution belongs to: the child fund
        named after the member's group (e.g. CAMP_1), parented to the campaign's
        department so it inherits its fund type and rolls up in trust/local
        reports. Created on demand. A blank group falls back to the parent fund."""
        from departments.models import Department
        from django.utils.text import slugify
        g = (group_name or "").strip()
        if not g:
            return self.department
        dept = Department.objects.filter(name__iexact=g).first()
        if dept:
            # adopt an orphan fund of the same name under this campaign
            if dept.parent_id is None and dept.pk != self.department_id:
                dept.parent = self.department
                dept.save()
            return dept
        base = (slugify(g) or "campgrp")[:46]
        slug, i = base, 2
        while Department.objects.filter(slug=slug).exists():
            slug = f"{base}-{i}"
            i += 1
        # fund_type / is_trust are inherited from the parent in Department.save()
        return Department.objects.create(
            name=g[:80], slug=slug, parent=self.department,
            category=self.department.category, selectable=True, active=True)

    def __str__(self):
        return self.name


class CampaignMember(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="members")
    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120, db_index=True, editable=False)
    phone = models.CharField(max_length=12, blank=True, db_index=True)
    group = models.CharField(max_length=40, blank=True)

    def save(self, *args, **kwargs):
        from members.models import name_key as _nk, normalize_phone
        self.name = (self.name or "").strip()[:120]
        self.name_key = _nk(self.name)
        # store a normalised 12-digit phone or nothing — never an over-long or
        # malformed value (a numeric cell can arrive as e.g. "2547...0"); matching
        # by phone needs the canonical form anyway, and name matching still works.
        self.phone = (normalize_phone(self.phone) or "")[:12]
        self.group = (self.group or "").strip()[:40]
        super().save(*args, **kwargs)

    class Meta:
        indexes = [models.Index(fields=["campaign", "name_key"]),
                   models.Index(fields=["campaign", "phone"])]
