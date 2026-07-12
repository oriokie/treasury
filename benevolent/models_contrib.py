"""Phase 4 — the Contribution Engine & Intelligent Allocation.

Two ideas hold this phase together. Both are about not lying.

**1. Money and obligations are different things, and they need different homes.**

A church welfare scheme deals in two currencies at once:

    MONEY        actually moved. It is receipted, it is in the bank, it is in
                 the general ledger. A contribution, a fee, a refund.
    OBLIGATION   what a member OWES. A penalty charged. A due waived. A debt
                 written off. Nothing has moved; nobody has paid anything.

Confusing them is the classic way a member ledger goes quietly wrong. Booking a
waiver as income inflates the fund by money nobody gave. Booking a penalty as
income recognises revenue that may never arrive. Booking a refund as negative
income hides a real payment from the cash book.

So:

    money       → the EXISTING documents. `giving.Transaction` for money in,
                  `cashbook.Expense` for money out. No new machinery, exactly as
                  in Phases 1–3.
    obligations → `MemberAdjustment` (this module). Nothing posts. Nothing
                  touches the ledger. It changes what `arrears_for()` says a
                  member owes, and that is all it does.

There is one function that answers "what does this member owe", and after this
phase it answers it from three inputs — the policy's dues, the adjustments
ledger, and the money actually received — and still from one place.

**2. Unallocated is not unrecorded.**

Intelligent allocation can fail, and it must be allowed to. What it must NEVER do
is lose the money. A receipt whose owner we cannot identify is still receipted,
still in the scheme's fund, still in the general ledger, still on the bank
reconciliation, still in the board pack. It sits in an intake queue waiting for a
human to say whose it is — and the fund balance is right the whole time.

A system that refused to bank money it could not attribute would be worse than one
with no matching at all.
"""
import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords


# ---------------------------------------------------------------------------
# Configurable narration rules
# ---------------------------------------------------------------------------

class ContributionRule(models.Model):
    """A configurable narration pattern that says "this money is scheme money".

    Mirrors `giving.AllocationRule` and `loans.LoanNarrationPattern` deliberately,
    down to the match-type semantics, and runs against the SAME normalised
    reference the allocation engine produces. This is a companion to that engine,
    not a second parser — a church that has learned how its members write "BEN"
    should not have to teach it twice.

    A rule identifies the SCHEME and the KIND of money. It does not identify the
    MEMBER: that is the allocator's job, and it has far better evidence to work
    with (phone numbers, membership numbers, case references) than a keyword.
    """

    class MatchType(models.TextChoices):
        EXACT = "EXACT", "Matches exactly"
        STARTS = "STARTS", "Starts with"
        ENDS = "ENDS", "Ends with"
        CONTAINS = "CONTAINS", "Contains"
        REGEX = "REGEX", "Matches a pattern (regex)"

    pattern = models.CharField(
        max_length=60,
        help_text="Compared against the normalised reference (lowercased, spaces "
                  "removed) — the same text the main allocation engine sees.")
    match_type = models.CharField(max_length=8, choices=MatchType.choices,
                                  default=MatchType.CONTAINS)
    scheme = models.ForeignKey("BenevolentScheme", on_delete=models.CASCADE,
                               related_name="contribution_rules")
    kind = models.CharField(
        max_length=12, blank=True,
        help_text="The kind of money this narration means (dues, a levy, a "
                  "registration fee…). Leave blank to let the allocator work it out "
                  "from the rest of the evidence — which it usually can.")
    priority = models.IntegerField(
        default=0,
        help_text="Higher wins where two rules match. A specific rule ('benlevy') "
                  "should outrank a general one ('ben').")
    active = models.BooleanField(default=True, db_index=True)
    seeded = models.BooleanField(default=False, editable=False)
    source = models.CharField(
        max_length=8, default="MANUAL",
        help_text="MANUAL, SEEDED, or LEARNED — a rule the system proposed after a "
                  "treasurer allocated the same narration by hand three times.")
    hits = models.PositiveIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-priority", "-active", "pattern"]

    def __str__(self):
        return f"{self.pattern} → {self.scheme.code}"

    def matches(self, normalized):
        import re
        p = (self.pattern or "").strip().lower().replace(" ", "")
        if not p or not normalized:
            return False
        if self.match_type == self.MatchType.REGEX:
            try:
                return bool(re.search(self.pattern, normalized))
            except re.error:
                return False       # a malformed pattern never matches, and never crashes
        return ((self.match_type == self.MatchType.EXACT and normalized == p)
                or (self.match_type == self.MatchType.STARTS and normalized.startswith(p))
                or (self.match_type == self.MatchType.ENDS and normalized.endswith(p))
                or (self.match_type == self.MatchType.CONTAINS and p in normalized))


# ---------------------------------------------------------------------------
# The obligations ledger — penalties, waivers, write-offs, corrections
# ---------------------------------------------------------------------------

class MemberAdjustment(models.Model):
    """A change to what a member OWES. Not money. Nothing posts.

    This is the half of the contribution engine that has no accounting entry, and
    understanding why is the whole point of the model.

    A **penalty** charged is not income: nobody has paid it, and they may never.
    Recognising it as revenue would book money the church does not have. It is a
    charge against the member, and it becomes income — as an ordinary receipt —
    on the day it is actually paid, like everything else.

    A **waiver** is not an expense: no money left the church. The church simply
    stopped asking for it. Booking it as a payment would show a cash outflow that
    never happened.

    A **write-off** is the church accepting it will not be paid. Again: nothing
    moved.

    So none of these touch the general ledger, and none of them should. They move
    a single number — what `arrears_for()` says the member owes — and that number
    is not an accounting balance, it is a memorandum of a member's account.

    (The one that DOES touch the ledger is a refund: real money leaving the bank.
    That is a `cashbook.Expense`, and it is modelled as `ContributionRefund`.)
    """

    class Kind(models.TextChoices):
        PENALTY = "PENALTY", "Penalty charged"
        WAIVER = "WAIVER", "Dues waived"
        WRITE_OFF = "WRITE_OFF", "Debt written off"
        CREDIT = "CREDIT", "Credit to the member"
        CHARGE = "CHARGE", "Other charge"

    # kinds that INCREASE what the member owes
    DEBITS = [Kind.PENALTY, Kind.CHARGE]
    # kinds that REDUCE it
    CREDITS = [Kind.WAIVER, Kind.WRITE_OFF, Kind.CREDIT]

    membership = models.ForeignKey("SchemeMembership", on_delete=models.CASCADE,
                                   related_name="adjustments")
    kind = models.CharField(max_length=10, choices=Kind.choices, db_index=True)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Always positive. Whether it adds to or reduces what the member "
                  "owes follows from the KIND — a signed amount invites a treasurer "
                  "to type a minus sign and reverse the meaning by accident.")
    on = models.DateField(default=_dt.date.today, db_index=True)
    period_label = models.CharField(
        max_length=10, blank=True,
        help_text="The dues period this applies to, if it applies to one.")
    reason = models.TextField(
        help_text="Why. Required: a charge or a waiver without a recorded reason is "
                  "an unanswerable question at the next audit.")
    comments = models.TextField(
        blank=True,
        help_text="Anything supplementary — not a substitute for the reason above.")
    policy = models.ForeignKey(
        "SchemePolicy", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="The policy version in force when this was raised.")
    automated = models.BooleanField(
        default=False, db_index=True,
        help_text="Raised by a published policy rule (e.g. a reinstatement fee) "
                  "rather than a treasurer's own discretionary judgement. A member "
                  "has a right to know which.")

    case = models.ForeignKey("BenevolentCase", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="adjustments",
                             help_text="Where the charge or waiver arises from a case.")

    raised_by = models.ForeignKey("auth.User", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Waiving a debt or charging a penalty changes what a member owes, "
                  "so a second person approves it — the same rule as an exemption.")
    approved_at = models.DateTimeField(null=True, blank=True)
    reversed_on = models.DateField(null=True, blank=True)
    reversed_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-on", "-id"]
        indexes = [models.Index(fields=["membership", "-on"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount} — {self.membership}"

    @property
    def is_effective(self):
        """An UNAPPROVED adjustment changes nothing. Proposing that a member be
        fined does not fine them; proposing a waiver does not waive anything."""
        return self.approved_by_id is not None and self.reversed_on is None

    @property
    def signed(self):
        """What this does to the member's balance: + owes more, − owes less."""
        if not self.is_effective:
            return Decimal(0)
        return (self.amount if self.kind in self.DEBITS else -self.amount)

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError(
                "The amount must be positive. Whether it adds to or reduces what the "
                "member owes is decided by the kind, not by a minus sign.")
        if not (self.reason or "").strip():
            raise ValidationError("An adjustment must record why.")


# ---------------------------------------------------------------------------
# Refunds — the one thing here that IS money leaving
# ---------------------------------------------------------------------------

class ContributionRefund(models.Model):
    """Money genuinely returned to a member.

    Distinct from REVERSING a receipt, and the distinction matters:

      * A receipt that should never have existed — the wrong member, a duplicate,
        a bounced payment — is **reversed**. It was a mistake; the church never
        had that money; the record should say so. `Transaction.is_reversed`
        already does this, and the contribution index row stops counting the
        moment it is set (Phase 1).

      * A receipt that was CORRECT, where the church now hands money back — a
        member leaving a scheme that refunds contributions, an overpayment
        returned — is a **refund**. The money was really received and is really
        being paid out. Both facts belong in the cash book.

    Reversing a correct receipt to "cancel out" a refund would hide a real payment
    from the bank reconciliation and understate both income and expenditure. So a
    refund is an ordinary `cashbook.Expense`: it clears the usual approval, gets a
    voucher, appears on the payment register, and posts DR/CR like any other
    payment out of the fund.
    """

    membership = models.ForeignKey("SchemeMembership", on_delete=models.PROTECT,
                                   related_name="refunds")
    scheme = models.ForeignKey("BenevolentScheme", on_delete=models.PROTECT,
                               related_name="refunds")
    expense = models.OneToOneField(
        "cashbook.Expense", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="benevolent_refund",
        help_text="The payment voucher. THE authority on the amount, the date and "
                  "whether the money has actually gone.")
    reason = models.TextField()
    requested_by = models.ForeignKey("auth.User", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Refund {self.amount} to {self.membership.member.name}"

    # amount and date are read off the voucher, never copied — the same discipline
    # as BenevolentPayout. Two stored figures are two figures that can disagree.
    @property
    def amount(self):
        return self.expense.amount if self.expense_id else Decimal(0)

    @property
    def date(self):
        return self.expense.date if self.expense_id else None

    @property
    def status(self):
        return self.expense.get_status_display() if self.expense_id else "—"

    @property
    def effective(self):
        """Whether the money has actually gone — the same condition the fund
        balance itself uses, so the two can never disagree."""
        if not self.expense_id:
            return False
        from cashbook.models import Expense
        return self.expense.status in (Expense.Status.APPROVED, Expense.Status.PAID)


# ---------------------------------------------------------------------------
# The intake queue
# ---------------------------------------------------------------------------

class ContributionIntake(models.Model):
    """A receipt that is scheme money but is not yet attached to a member.

    THE MONEY IS ALREADY BANKED. It is already a `giving.Transaction`, already in
    the scheme's fund, already in the general ledger, already on the bank
    reconciliation and already in the board pack. What is missing is only the
    answer to "whose is it?".

    That distinction is the whole design of this queue. A system that refused to
    receipt money it could not attribute would produce a fund balance that
    disagreed with the bank, which is a far worse problem than an unattributed
    receipt. So allocation is allowed to fail, and when it does, the money is
    still right — it just sits here until somebody says whose it is.

    Every candidate the allocator considered is frozen onto the row, with the
    signals and the score, so a treasurer resolving it can see what the machine
    thought and why — and so a wrong auto-allocation can be understood after the
    fact rather than merely undone.
    """

    class Status(models.TextChoices):
        AUTO = "AUTO", "Allocated automatically"
        REVIEW = "REVIEW", "Needs review"
        UNMATCHED = "UNMATCHED", "No candidate found"
        DUPLICATE = "DUPLICATE", "Possible duplicate"
        RESOLVED = "RESOLVED", "Resolved by a treasurer"
        REJECTED = "REJECTED", "Not scheme money"

    OPEN_STATUSES = [Status.REVIEW, Status.UNMATCHED, Status.DUPLICATE]

    transaction = models.OneToOneField(
        "giving.Transaction", on_delete=models.CASCADE,
        related_name="benevolent_intake",
        help_text="The receipt. Already banked, already in the ledger.")
    scheme = models.ForeignKey("BenevolentScheme", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="intakes")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.REVIEW, db_index=True)
    confidence = models.PositiveSmallIntegerField(
        default=0, db_index=True,
        help_text="0–100. How sure the allocator was about its best candidate.")
    candidates = models.JSONField(
        default=list, blank=True,
        help_text="Every candidate considered, with its signals and score — frozen, "
                  "so a treasurer can see what the machine thought and why.")
    suggested_kind = models.CharField(max_length=12, blank=True)
    suggested_membership = models.ForeignKey(
        "SchemeMembership", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+")
    suggested_case = models.ForeignKey(
        "BenevolentCase", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+")
    duplicate_of = models.ForeignKey(
        "BenevolentContribution", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
        help_text="The contribution this looks like a repeat of.")

    contribution = models.ForeignKey(
        "BenevolentContribution", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", help_text="What it became, once resolved.")
    resolved_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        return f"Intake {self.transaction_id} ({self.get_status_display()})"

    @property
    def amount(self):
        return self.transaction.amount

    @property
    def date(self):
        return self.transaction.date

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES
