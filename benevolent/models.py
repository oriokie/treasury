"""Benevolent Scheme Engine — a configurable welfare-scheme platform.

Design intent
-------------
This is deliberately NOT "the benevolent fund". It is an engine that runs any
number of member welfare schemes (Benevolent, Medical, Education, Emergency
Relief …) off the same code, differing only by CONFIGURATION:

    BenevolentScheme      what the scheme is, and which fund holds its money
    SchemePolicy          the RULES, versioned and immutable once used
    SchemeBenefitRule     per-event-type benefit lines under a policy
    BenevolentEventType   the qualifying events a scheme recognises

Adding a Medical Fund therefore means creating a scheme + a policy, not writing
business logic. Every rule the engine can enforce is a field on SchemePolicy or
SchemeBenefitRule; the eligibility engine (services/eligibility.py) reads those
fields and nothing else.

Accounting: no new money machinery
----------------------------------
Following the loans module's precedent exactly, this module invents NO balance
maths of its own. Every shilling flows through the two existing source-document
types, so the general ledger, fund balances, bank reconciliation, budget and
every report tie out with no benevolent-specific accounting:

  Contribution in   a giving.Transaction CREDIT on the scheme's fund, attributed
                    to the contributing member. This IS income of a designated
                    local fund (unlike a loan receipt, which is financing), so
                    it posts DR Cash / CR Income exactly like any other receipt.

  Benefit paid out  a cashbook.Expense with category=BENEVOLENCE charged to the
                    scheme's fund. It posts DR Benevolence / CR Cash and runs
                    through the EXISTING expense approval workflow (including
                    dual approval, period locks and the payment register) — the
                    treasurer's control over money is not bypassed or weakened.

BenevolentContribution / BenevolentPayout are the scheme-side INDEX over those
documents. The documents remain authoritative: an index row only counts while
the document it points at still counts ("effective"), so reversing a receipt or
rejecting an expense flows straight through to the scheme's figures with no
reconciliation step. Amounts and dates are read from the document rather than
copied, so the two can never drift apart.

A scheme's cash balance is its FUND's balance, taken from the Financial Metrics
Registry — it is never recomputed here.

Immutability
------------
* A SchemePolicy version becomes LOCKED the moment a case is assessed under it.
  A locked policy's rules can never be edited: changing the rules means
  publishing a NEW version, which applies only from its effective date forward.
* Every BenevolentCase freezes the policy terms it was decided under into
  `policy_snapshot`, and the eligibility evaluation into `eligibility_snapshot`.
  Even if a policy row were somehow altered, the case still carries the terms it
  was actually assessed on — historical decisions are reproducible forever.
"""
import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.text import slugify
from simple_history.models import HistoricalRecords

# Phase 3 registry models. Imported at the TOP, not the bottom, because
# SchemeMembership uses Standing.choices and RegistrationType.choices at
# class-definition time. models_registry refers to this module only by string FK,
# so there is no cycle.
from benevolent.models_registry import (  # noqa: E402,F401
    MembershipEvent, MembershipExemption, RegistrationType, Standing)


# ---------------------------------------------------------------------------
# Numbering — permanent, never-reused references (same guarantee as
# JournalSequence / LoanSequence: a number identifies one thing forever).
# ---------------------------------------------------------------------------

class _YearSequence(models.Model):
    """Abstract base: one row per year, a counter that only ever increases."""
    year = models.PositiveSmallIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    prefix = "XX"

    class Meta:
        abstract = True

    @classmethod
    def next_number(cls, year, prefix=None):
        from django.db import transaction
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(year=year)
            seq.last_number += 1
            seq.save(update_fields=["last_number"])
            return f"{prefix or cls.prefix}-{year}-{seq.last_number:04d}"


class MembershipSequence(_YearSequence):
    prefix = "BM"


class CaseSequence(_YearSequence):
    prefix = "BC"


# ---------------------------------------------------------------------------
# The scheme
# ---------------------------------------------------------------------------

class BenevolentScheme(models.Model):
    """One welfare scheme. Its behaviour comes entirely from its policy; this
    row says what the scheme is called, who administers it, and which fund
    holds its money."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft (not yet open)"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended (no new cases)"
        CLOSED = "CLOSED", "Closed"

    class Kind(models.TextChoices):
        """A presentation label only — it changes wording and icons, never a
        single rule. Rules live exclusively in the policy, which is the whole
        point of the engine: a Medical Fund is a scheme with a medical policy,
        not a new code path."""
        BENEVOLENT = "BENEVOLENT", "Benevolent / bereavement"
        MEDICAL = "MEDICAL", "Medical"
        EDUCATION = "EDUCATION", "Education"
        EMERGENCY = "EMERGENCY", "Emergency relief"
        OTHER = "OTHER", "Other welfare scheme"

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    code = models.CharField(
        max_length=8, unique=True,
        help_text="Short reference used on membership and case numbers (e.g. BEN, MED).")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.BENEVOLENT,
                            help_text="A label for wording only — every rule comes from the policy.")
    description = models.TextField(blank=True)

    fund = models.ForeignKey(
        "departments.Department", on_delete=models.PROTECT, related_name="benevolent_schemes",
        help_text="The fund that holds this scheme's money. Contributions are "
                  "receipted into it and benefits are paid out of it, so the "
                  "scheme's cash balance IS this fund's balance — never a "
                  "separately-maintained figure.")

    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.DRAFT, db_index=True)
    opened_on = models.DateField(null=True, blank=True)
    closed_on = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="benevolent_schemes_created")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:50]
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)

    def clean(self):
        # Two live schemes on one fund would make "the scheme's balance"
        # ambiguous — the fund balance could not be attributed to either.
        if self.fund_id:
            clash = (BenevolentScheme.objects
                     .filter(fund_id=self.fund_id)
                     .exclude(status=self.Status.CLOSED)
                     .exclude(pk=self.pk).first())
            if clash and self.status != self.Status.CLOSED:
                raise ValidationError(
                    f"'{clash.name}' already uses the {clash.fund.name} fund. Each live "
                    f"scheme needs its own fund so its balance is unambiguous.")
        if self.fund_id and self.fund.fund_type == self.fund.FundType.TRUST:
            raise ValidationError(
                "A scheme's fund must be a Local fund. A Trust fund is remitted to "
                "the field and is not the church's to pay benefits from.")

    # ---- state -----------------------------------------------------------
    @property
    def is_open(self):
        """Can new cases be raised against this scheme right now?"""
        return self.status == self.Status.ACTIVE

    @property
    def accepts_contributions(self):
        return self.status in (self.Status.ACTIVE, self.Status.SUSPENDED)

    # ---- policy ----------------------------------------------------------
    def policy_on(self, on=None):
        """The policy version in force on a date — the ONE resolution rule the
        whole engine uses.

        A SUPERSEDED version is still the version that was in force during its
        own window, and must resolve for dates inside it: that is the entire
        point of versioning. (Filtering on status=ACTIVE alone was a real bug —
        it meant that as soon as a new version was published, every past date
        resolved to nothing, and a late-reported claim would have been refused
        for "no policy in force" instead of being decided by the rules that
        actually applied when the event happened.)

        WITHDRAWN and DRAFT versions never resolve: they were never in force.

        Never guesses: if no policy was in force on that date, the answer is
        None and the caller must handle it — a case cannot be assessed under no
        policy.
        """
        from django.db.models import Q
        on = on or _dt.date.today()
        return (self.policies
                .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on),
                        status__in=[SchemePolicy.Status.ACTIVE,
                                    SchemePolicy.Status.SUPERSEDED],
                        effective_from__lte=on)
                .order_by("-effective_from", "-version").first())

    @property
    def current_policy(self):
        return self.policy_on(_dt.date.today())

    @property
    def next_version(self):
        top = self.policies.order_by("-version").values_list("version", flat=True).first()
        return (top or 0) + 1

    # ---- figures (delegated: never recomputed here) ----------------------
    @property
    def balance(self):
        """The scheme's cash balance = its fund's balance, from the registry."""
        from core.metrics import metrics
        return metrics.fund_balance(self.fund)

    @property
    def active_member_count(self):
        return self.memberships.filter(status=SchemeMembership.Status.ACTIVE).count()

    @property
    def open_case_count(self):
        return self.cases.filter(status__in=BenevolentCase.OPEN_STATUSES).count()


class BenevolentEventType(models.Model):
    """A qualifying event a scheme recognises (bereavement of a spouse,
    hospitalisation, school fees, fire loss …). Scheme-scoped, so each scheme
    defines its own vocabulary without polluting the others."""

    scheme = models.ForeignKey(BenevolentScheme, on_delete=models.CASCADE,
                               related_name="event_types")
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20,
                            help_text="Stable key used by benefit rules and integrations.")
    description = models.CharField(max_length=200, blank=True)
    covers_dependants = models.BooleanField(
        default=True,
        help_text="Whether this event can be claimed for a registered dependant "
                  "rather than the member themselves.")
    requires_document = models.BooleanField(
        default=False,
        help_text="A supporting document (burial permit, medical report…) must be "
                  "attached before the case can be approved.")
    required_documents = models.JSONField(
        default=list, blank=True,
        help_text="Named documents this event needs (e.g. 'Burial permit', "
                  "'Death certificate'), one per line in the form. A case shows "
                  "each as a checklist item rather than a single yes/no. Leave "
                  "empty to fall back to the plain requires_document toggle above "
                  "(at least one attachment of any kind).")
    sort_order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["scheme", "code"],
                                               name="uniq_event_code_per_scheme")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.code = (self.code or slugify(self.name)).strip().upper().replace("-", "_")[:20]
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# The policy — versioned, immutable once used
# ---------------------------------------------------------------------------

class SchemePolicy(models.Model):
    """One immutable, versioned statement of a scheme's rules.

    Editing rules is not a thing you do: you PUBLISH A NEW VERSION with a later
    effective date. Old cases keep pointing at the version they were decided
    under, and `policy_on(date)` resolves any historical date correctly, so
    changing the rules can never retrospectively alter a past decision.

    Once any case has been assessed under a version, the version is LOCKED and
    its rule fields are enforced read-only at the model layer (see save()).
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft (not yet in force)"
        ACTIVE = "ACTIVE", "Active"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        WITHDRAWN = "WITHDRAWN", "Withdrawn (never used)"

    class ContributionMode(models.TextChoices):
        NONE = "NONE", "No contributions (funded from elsewhere)"
        VOLUNTARY = "VOLUNTARY", "Voluntary giving"
        FIXED_PERIODIC = "FIXED_PERIODIC", "Fixed periodic dues"
        PER_CASE_LEVY = "PER_CASE_LEVY", "Levy raised per case"
        HYBRID = "HYBRID", "Hybrid — periodic dues AND a levy per case"

    class Frequency(models.TextChoices):
        MONTHLY = "MONTHLY", "Monthly"
        QUARTERLY = "QUARTERLY", "Quarterly"
        ANNUAL = "ANNUAL", "Annual"

    class BenefitMode(models.TextChoices):
        FIXED = "FIXED", "One fixed benefit for every event"
        SCHEDULE = "SCHEDULE", "A schedule of benefits, per event type"
        PERCENTAGE = "PERCENTAGE", "A percentage of the assessed cost"
        DISCRETIONARY = "DISCRETIONARY", "Discretionary, within a cap"
        POOLED = "POOLED", "Whatever the levy for this case collects"
        PER_MEMBER_MULTIPLE = "PER_MEMBER_MULTIPLE", "The levy × the active membership"

    class ArrearsTreatment(models.TextChoices):
        IGNORE = "IGNORE", "Ignore arrears"
        BLOCK = "BLOCK", "Block the claim"
        DEDUCT = "DEDUCT", "Pay, but deduct the arrears from the benefit"

    class RegistrationApproval(models.TextChoices):
        AUTO = "AUTO", "Automatic on enrolment"
        TREASURER = "TREASURER", "A treasurer admits the member"
        COMMITTEE = "COMMITTEE", "The committee admits the member"

    class RenewalPeriod(models.TextChoices):
        NONE = "NONE", "No renewal"
        ANNUAL = "ANNUAL", "Every year"
        BIENNIAL = "BIENNIAL", "Every two years"

    class ApprovalMode(models.TextChoices):
        TREASURER = "TREASURER", "A treasurer approves"
        COMMITTEE = "COMMITTEE", "The committee approves (by quorum)"
        TWO_STAGE = "TWO_STAGE", "Treasurer below the threshold, committee above it"

    class InactivityAction(models.TextChoices):
        NONE = "NONE", "Do nothing"
        FLAG = "FLAG", "Flag as inactive (cover continues)"
        SUSPEND = "SUSPEND", "Suspend (cover stops until they return)"
        LAPSE = "LAPSE", "Lapse the membership"
        EXPEL = "EXPEL", "Remove from the scheme"

    class HouseholdMode(models.TextChoices):
        INDIVIDUAL = "INDIVIDUAL", "Each member enrols individually"
        HOUSEHOLD = "HOUSEHOLD", "One enrolment covers the whole household"

    class InheritanceMode(models.TextChoices):
        NONE = "NONE", "No inheritance — the membership simply ends"
        NOMINEE = "NOMINEE", "Pay the member's nominees, in their recorded shares"
        NEXT_OF_KIN = "NEXT_OF_KIN", "Pay the next of kin named on the case"
        HOUSEHOLD = "HOUSEHOLD", "The household succeeds to the membership"

    class Rounding(models.TextChoices):
        NONE = "NONE", "No rounding"
        TEN = "TEN", "To the nearest 10"
        HUNDRED = "HUNDRED", "To the nearest 100"
        THOUSAND = "THOUSAND", "To the nearest 1,000"

    # Funding methods are a multi-select, held as a JSON list of these codes. It
    # is a rule, not a note: it governs what a scheme is ALLOWED to be funded by,
    # so a treasurer cannot quietly start subsidising a member-funded scheme out
    # of the church budget without the constitution being changed to permit it.
    FUNDING_METHODS = [
        ("DUES", "Member dues"),
        ("LEVY", "Per-case levies"),
        ("DONATION", "Donations and gifts"),
        ("SUBSIDY", "Church subsidy (a transfer from another fund)"),
        ("FUNDRAISING", "Fundraising events"),
        ("INVESTMENT", "Investment or interest income"),
    ]

    # ---- the fields that ARE the rules. Anything listed here is frozen once
    # the version is locked, and is what terms_snapshot() captures.
    #
    # Phase 2 grew this list from 19 to 54. Every addition passes the same test:
    # *does it decide an outcome?* Registration requirements, renewal rules,
    # committee quorums, bereaved exemptions, inactivity actions, household
    # cover and inheritance all change whether a claim qualifies or what it is
    # worth — so they are RULES, and belong here, under the version lock, rather
    # than in the settings area where editing one would silently rewrite the
    # basis of decisions already made. (Things that steer operations but decide
    # nothing — accounting mappings, notification preferences, automation —
    # live on BenevolentSettings, and are freely editable.)
    RULE_FIELDS = [
        "effective_from",
        # membership & eligibility
        "membership_required", "waiting_period_days", "min_contributions",
        "arrears_block", "max_arrears_allowed", "arrears_treatment",
        # registration
        "registration_required", "registration_approval", "registration_fee",
        "registration_fee_refundable", "min_age", "max_age",
        "require_registration_form", "require_id_document",
        # renewals
        "renewal_required", "renewal_period", "renewal_fee", "renewal_month",
        "renewal_grace_days", "lapse_on_non_renewal",
        # contributions & funding
        "contribution_mode", "contribution_amount", "contribution_frequency",
        "levy_amount", "max_levies_per_year", "funding_methods", "joining_fee",
        # benefits
        "benefit_mode", "benefit_amount", "benefit_percent",
        "benefit_cap", "benefit_floor", "benefit_rounding",
        # approvals
        "approval_mode", "committee_threshold", "committee_quorum",
        "committee_requires_chair",
        # bereaved-member rules
        "bereaved_contribution_policy", "bereaved_reduction_percent",
        "bereaved_deduct_own_levy", "bereaved_dues_waiver_months",
        # inactivity
        "inactivity_months", "inactivity_action", "reinstatement_fee",
        "reinstatement_waiting_days", "inactivity_missed_cases",
        # standing (Phase 3)
        "grace_period_days", "allow_exemptions", "exemption_age",
        # registry (Phase 3)
        "allow_transfers", "max_household_size",
        # household
        "household_mode", "max_dependants", "dependant_age_limit",
        "spouse_auto_covered",
        # inheritance
        "inheritance_mode", "transfer_membership_on_death",
        "refund_contributions_on_exit", "refund_percent",
        # claims
        "claim_window_days", "max_claims_per_year", "max_benefit_per_year",
        "require_documents", "require_different_approver", "allow_override",
    ]

    scheme = models.ForeignKey(BenevolentScheme, on_delete=models.PROTECT,
                               related_name="policies")
    version = models.PositiveIntegerField(editable=False)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.DRAFT, db_index=True)
    effective_from = models.DateField(
        db_index=True,
        help_text="The date these rules take effect. Cases with an event on or "
                  "after this date (and before the next version) are decided by them.")
    effective_to = models.DateField(
        null=True, blank=True, editable=False,
        help_text="Set automatically when a later version supersedes this one.")

    # ---- Membership rules ------------------------------------------------
    membership_required = models.BooleanField(
        default=True, help_text="Only enrolled members may claim.")
    waiting_period_days = models.PositiveIntegerField(
        default=0,
        help_text="Days a member must have been enrolled before a claim qualifies. "
                  "0 = cover starts immediately.")
    min_contributions = models.PositiveIntegerField(
        default=0, help_text="Minimum number of contributions before a claim qualifies.")
    arrears_block = models.BooleanField(
        default=False, help_text="A member in arrears cannot claim.")
    max_arrears_allowed = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="If arrears block: how much a member may still owe and remain eligible.")

    # ---- Contribution rules ----------------------------------------------
    contribution_mode = models.CharField(max_length=16, choices=ContributionMode.choices,
                                         default=ContributionMode.FIXED_PERIODIC)
    contribution_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="The dues amount per period, or the levy per case.")
    contribution_frequency = models.CharField(
        max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY,
        help_text="How often fixed dues fall due.")
    joining_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # ---- Benefit rules ---------------------------------------------------
    benefit_mode = models.CharField(max_length=20, choices=BenefitMode.choices,
                                    default=BenefitMode.FIXED)
    benefit_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="The benefit paid, when the mode is a single fixed benefit.")
    benefit_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Percentage of the assessed cost paid, when the mode is percentage.")
    benefit_cap = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Maximum payable for one case. Blank = no cap.")
    benefit_floor = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Minimum payable for one qualifying case. Blank = no floor.")

    arrears_treatment = models.CharField(
        max_length=8, choices=ArrearsTreatment.choices, default=ArrearsTreatment.IGNORE,
        help_text="What arrears do to a claim. DEDUCT pays the benefit but nets the "
                  "arrears off it — which is how most schemes actually behave, and is "
                  "kinder than refusing a bereaved family outright.")

    # ---- Registration -------------------------------------------------------
    registration_required = models.BooleanField(
        default=False,
        help_text="A member must be formally registered (not merely listed) before cover "
                  "begins.")
    registration_approval = models.CharField(
        max_length=10, choices=RegistrationApproval.choices,
        default=RegistrationApproval.AUTO,
        help_text="Who admits a new member.")
    registration_fee = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="A one-off fee on joining, separate from any dues.")
    registration_fee_refundable = models.BooleanField(default=False)
    min_age = models.PositiveSmallIntegerField(
        default=0, help_text="Minimum age to join. 0 = no minimum.")
    max_age = models.PositiveSmallIntegerField(
        default=0, help_text="Maximum age to join. 0 = no maximum.")
    require_registration_form = models.BooleanField(
        default=False, help_text="A signed application form must be on file.")
    require_id_document = models.BooleanField(
        default=False, help_text="A copy of an identity document must be on file.")

    # ---- Renewals -----------------------------------------------------------
    renewal_required = models.BooleanField(default=False)
    renewal_period = models.CharField(
        max_length=8, choices=RenewalPeriod.choices, default=RenewalPeriod.NONE)
    renewal_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    renewal_month = models.PositiveSmallIntegerField(
        default=1,
        help_text="The month renewal falls due (1 = January). Every membership renews "
                  "together, which is how a church actually runs a subscription year.")
    renewal_grace_days = models.PositiveIntegerField(
        default=30, help_text="How long after renewal falls due a member stays covered.")
    lapse_on_non_renewal = models.BooleanField(
        default=True,
        help_text="A membership that is not renewed within the grace period lapses.")

    # ---- Contributions ------------------------------------------------------
    levy_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="The levy each member pays PER CASE. Used by the per-case and hybrid "
                  "contribution modes (a hybrid scheme charges dues AND this).")
    max_levies_per_year = models.PositiveIntegerField(
        default=0,
        help_text="The most levies one member can be asked for in a year — the protection "
                  "against a bad year bankrupting the membership. 0 = no limit.")
    funding_methods = models.JSONField(
        default=list, blank=True,
        help_text="What this scheme is permitted to be funded by. A rule, not a note: it "
                  "stops a member-funded scheme being quietly subsidised out of the church "
                  "budget without the constitution being changed to allow it.")

    # ---- Approvals ----------------------------------------------------------
    approval_mode = models.CharField(
        max_length=10, choices=ApprovalMode.choices, default=ApprovalMode.TREASURER)
    committee_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Under two-stage approval: benefits at or above this need the committee; "
                  "below it a treasurer may approve alone.")
    committee_quorum = models.PositiveSmallIntegerField(
        default=3,
        help_text="How many committee members must record an approval before a benefit is "
                  "authorised.")
    committee_requires_chair = models.BooleanField(
        default=False,
        help_text="An approval level, not just a headcount: when set, the quorum is not "
                  "reached by any N approvals — one of them must be the Chair's. Only "
                  "meaningful where the scheme has a committee roster with a Chair seat "
                  "(see Committee membership); ignored otherwise, since there is no Chair "
                  "to require.")

    class BereavedContributionPolicy(models.TextChoices):
        """The four ways a constitution answers one question: does the member
        who is themselves the reason for a case still have to pay into it?

        Phase 2 modelled this as two overlapping booleans
        (`bereaved_exempt_own_levy` / `bereaved_deduct_own_levy`), which could
        not express "pay a reduced amount" or "let the committee decide" at
        all, and which — audited while building this — had a live bug: a
        "deduct" member was left on the levy roster AND had the same amount
        taken off their benefit, charging them twice. This replaces both
        booleans with one explicit choice, covering exactly the four cases a
        real constitution actually distinguishes.
        """
        CONTRIBUTES = "CONTRIBUTES", "Contributes like any other member"
        REDUCED = "REDUCED", "Contributes a reduced amount"
        EXEMPT = "EXEMPT", "Automatically exempt"
        COMMITTEE_DECIDES = "COMMITTEE_DECIDES", "The committee decides, case by case"

    # ---- Bereaved-member rules ---------------------------------------------
    bereaved_contribution_policy = models.CharField(
        max_length=18, choices=BereavedContributionPolicy.choices,
        default=BereavedContributionPolicy.EXEMPT,
        help_text="Whether the member a case is FOR still has to pay into it — "
                  "the levy round, or their own dues. EXEMPT is what almost every "
                  "real constitution says: asking a grieving family to chip in "
                  "for their own benefit is the thing schemes explicitly write "
                  "down that they do not do.")
    bereaved_reduction_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("50"),
        help_text="Under REDUCED, what percentage of the normal amount the "
                  "bereaved member is asked for.")
    bereaved_deduct_own_levy = models.BooleanField(
        default=False,
        help_text="Where the bereaved member DOES contribute (CONTRIBUTES or "
                  "REDUCED), collect it by taking it out of their own benefit "
                  "rather than asking them to pay it up front like everyone "
                  "else. They are then left OFF the levy roster — being asked "
                  "to pay in AND having it deducted would charge them twice.")
    bereaved_dues_waiver_months = models.PositiveSmallIntegerField(
        default=0,
        help_text="Months of ordinary periodic dues waived for a member after "
                  "their own case — independent of the levy question above; a "
                  "scheme can waive dues, exempt the levy, both, or neither. "
                  "0 = no waiver. Where this applies and the levy policy is "
                  "EXEMPT, the waiver is granted as a visible, auditable "
                  "exemption (see benevolent.services.registry) rather than a "
                  "silent adjustment to arrears — a member has a right to see "
                  "why they owe nothing, the same as for any other exemption.")

    # ---- Inactivity ---------------------------------------------------------
    inactivity_months = models.PositiveSmallIntegerField(
        default=0,
        help_text="A member who has not contributed for this many months is inactive. "
                  "0 = never treat anyone as inactive.")
    inactivity_action = models.CharField(
        max_length=8, choices=InactivityAction.choices, default=InactivityAction.NONE)
    reinstatement_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reinstatement_waiting_days = models.PositiveIntegerField(
        default=0,
        help_text="A reinstated member serves this waiting period again before they can "
                  "claim. Stops a lapsed member rejoining the week a relative falls ill.")
    inactivity_missed_cases = models.PositiveSmallIntegerField(
        default=0,
        help_text="A member who fails to contribute to this many consecutive case levies "
                  "is inactive. The measure that matters in a levy scheme, where there "
                  "are no monthly dues to miss — the member who never stands with a "
                  "bereaved family, and then expects the family to stand with them. "
                  "0 = do not measure this.")

    # ---- Standing (Phase 3) -------------------------------------------------
    grace_period_days = models.PositiveIntegerField(
        default=0,
        help_text="How long a member who has fallen behind stays in GOOD STANDING before "
                  "they are counted as in arrears. A grace period is a promise that cover "
                  "does not evaporate the day a payment is late — and while it lasts, the "
                  "member is covered. 0 = no grace.")
    allow_exemptions = models.BooleanField(
        default=True,
        help_text="Members may be formally excused from contributing (life members, the "
                  "very old, genuine hardship). Turning this off means no one can be, "
                  "which some constitutions do insist on.")
    exemption_age = models.PositiveSmallIntegerField(
        default=0,
        help_text="Members at or over this age are automatically exempt from dues, with no "
                  "paperwork. 0 = no automatic age exemption.")

    # ---- Registry (Phase 3) -------------------------------------------------
    allow_transfers = models.BooleanField(
        default=True,
        help_text="A membership may be passed to a successor (usually the surviving spouse) "
                  "keeping its original joining date, so the years already paid in are not "
                  "lost by the household.")
    max_household_size = models.PositiveSmallIntegerField(
        default=0,
        help_text="The most people one household registration may cover, including the "
                  "principal member. 0 = no limit.")

    # ---- Household ----------------------------------------------------------
    household_mode = models.CharField(
        max_length=10, choices=HouseholdMode.choices, default=HouseholdMode.INDIVIDUAL)
    max_dependants = models.PositiveSmallIntegerField(
        default=0, help_text="The most dependants one membership may register. 0 = no limit.")
    dependant_age_limit = models.PositiveSmallIntegerField(
        default=0,
        help_text="A child dependant is covered until this age. 0 = no age limit.")
    spouse_auto_covered = models.BooleanField(
        default=False,
        help_text="A registered spouse is covered without being counted against the "
                  "dependant limit.")

    # ---- Inheritance --------------------------------------------------------
    inheritance_mode = models.CharField(
        max_length=12, choices=InheritanceMode.choices, default=InheritanceMode.NONE,
        help_text="Who receives a benefit on the member's own death, and what becomes of "
                  "their membership.")
    transfer_membership_on_death = models.BooleanField(
        default=False,
        help_text="The successor takes over the membership KEEPING its original joining "
                  "date, so no new waiting period is served — the years the deceased paid "
                  "in are not lost by the household.")
    refund_contributions_on_exit = models.BooleanField(
        default=False,
        help_text="A member leaving the scheme gets some of their contributions back.")
    refund_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="What percentage of contributions is refunded on exit.")

    benefit_rounding = models.CharField(
        max_length=8, choices=Rounding.choices, default=Rounding.NONE,
        help_text="Round the computed benefit — useful where a pooled or percentage "
                  "calculation produces an awkward figure to hand over.")

    # ---- Claim rules -----------------------------------------------------
    claim_window_days = models.PositiveIntegerField(
        default=0,
        help_text="A case must be reported within this many days of the event. 0 = no limit.")
    max_claims_per_year = models.PositiveIntegerField(
        default=0, help_text="Maximum cases one membership may claim per calendar year. 0 = no limit.")
    max_benefit_per_year = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Maximum total benefit one membership may receive per calendar year. 0 = no limit.")
    require_documents = models.BooleanField(
        default=False, help_text="Every case must have a supporting document attached.")
    require_different_approver = models.BooleanField(
        default=True,
        help_text="A benefit must be approved by someone other than the person who "
                  "raised the case — segregation of duties for a money decision. "
                  "Switch this off only for a scheme small enough that the same "
                  "person genuinely has to do both (e.g. a single treasurer with no "
                  "assistant); the recommended, safer default is to leave it on. "
                  "Never applies where approval is routed to the committee — a "
                  "committee decision already requires more than one person by its "
                  "own quorum, regardless of this setting.")
    allow_override = models.BooleanField(
        default=True,
        help_text="An approver may pay a case that fails an eligibility check, provided "
                  "they record a written reason (which is kept on the audit trail).")

    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="benevolent_policies_created")
    published_by = models.ForeignKey("auth.User", null=True, blank=True,
                                     on_delete=models.SET_NULL,
                                     related_name="benevolent_policies_published")
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["scheme", "-version"]
        constraints = [models.UniqueConstraint(fields=["scheme", "version"],
                                               name="uniq_policy_version_per_scheme")]
        indexes = [models.Index(fields=["scheme", "status", "effective_from"])]

    def __str__(self):
        return f"{self.scheme.code} policy v{self.version}"

    # ---- immutability ----------------------------------------------------
    @property
    def is_locked(self):
        """A version is locked once a case has been decided under it (or it has
        been superseded). From then on its rules are historical fact."""
        if not self.pk:
            return False
        if self.status == self.Status.SUPERSEDED:
            return True
        return self.cases.exists()

    def terms_snapshot(self):
        """Every rule, as plain JSON-safe data. Frozen into each case so the
        decision is reproducible even if this row were later tampered with."""
        out = {"policy_version": self.version, "scheme": self.scheme.code}
        for f in self.RULE_FIELDS:
            v = getattr(self, f)
            if isinstance(v, Decimal):
                v = str(v)
            elif isinstance(v, _dt.date):
                v = v.isoformat()
            out[f] = v
        out["benefit_rules"] = [
            {"event_type": r.event_type.code, "event_name": r.event_type.name,
             "amount": str(r.amount), "percent": str(r.percent),
             "cap": (str(r.cap) if r.cap is not None else None),
             "waiting_period_days": r.waiting_period_days,
             "max_per_year": r.max_per_year}
            for r in self.benefit_rules.select_related("event_type").filter(active=True)
        ]
        return out

    def save(self, *args, **kwargs):
        if not self.version:
            self.version = self.scheme.next_version
        if self.pk:
            prior = SchemePolicy.objects.filter(pk=self.pk).first()
            if prior and prior.is_locked:
                changed = [f for f in self.RULE_FIELDS
                           if getattr(prior, f) != getattr(self, f)]
                if changed:
                    raise ValidationError(
                        f"{prior} has already been used to decide a case, so its rules are "
                        f"permanently fixed ({', '.join(changed)} cannot change). Publish a "
                        f"new version instead — it will apply from its effective date "
                        f"forward and will not disturb any decision already made.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_locked:
            raise ValidationError(
                f"{self} has decided cases and is part of the audit record; it cannot be "
                f"deleted. Withdraw or supersede it instead.")
        return super().delete(*args, **kwargs)

    # ---- rule lookup -----------------------------------------------------
    def rule_for(self, event_type):
        """The benefit line for an event type, or None (which under SCHEDULE
        mode means the event is not covered)."""
        if event_type is None:
            return None
        return self.benefit_rules.filter(event_type=event_type, active=True).first()


class SchemeBenefitRule(models.Model):
    """A benefit line under a policy: what one event type is worth. This is what
    makes a per-event benefit schedule (a Medical Fund's tiers, or bereavement
    amounts differing by relationship) pure configuration."""

    policy = models.ForeignKey(SchemePolicy, on_delete=models.CASCADE,
                               related_name="benefit_rules")
    event_type = models.ForeignKey(BenevolentEventType, on_delete=models.PROTECT,
                                   related_name="benefit_rules")
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Benefit paid for this event (used by FIXED and SCHEDULE modes).")
    percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Percentage of assessed cost for this event (PERCENTAGE mode).")
    cap = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Per-case cap for this event. Overrides the policy cap when set.")
    waiting_period_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Waiting period for this event only. Blank = use the policy's.")
    max_per_year = models.PositiveIntegerField(
        default=0, help_text="Maximum claims of this event type per membership per year. 0 = no limit.")
    active = models.BooleanField(default=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["event_type__sort_order", "event_type__name"]
        constraints = [models.UniqueConstraint(fields=["policy", "event_type"],
                                               name="uniq_benefit_rule_per_event")]

    def __str__(self):
        return f"{self.event_type.name}: {self.amount}"

    def save(self, *args, **kwargs):
        if self.policy_id and self.policy.is_locked:
            raise ValidationError(
                f"{self.policy} has already decided cases; its benefit schedule is fixed. "
                f"Publish a new policy version to change what an event is worth.")
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

class SchemeMembership(models.Model):
    """A church member's enrolment in one scheme. A member may belong to several
    schemes; each enrolment is separate, with its own dates and standing.

    TWO AXES (Phase 3). `status` is the administrative LIFECYCLE — what a human
    decided. `standing` is COMPUTED — where the member stands under the policy.
    See benevolent/models_registry.py for why they were separated, and
    benevolent/services/standing.py for the function that derives the second.
    """

    class Status(models.TextChoices):
        """The administrative lifecycle. A HUMAN sets every one of these.

        Automation cannot write here at all — not because it is told not to, but
        because it writes to `standing` and this field is not `standing`. That is
        a structural guarantee rather than a rule someone has to remember.

        Note what is NOT here any more: LAPSED, EXPIRED and INACTIVE. Those were
        never decisions — they were derived facts wearing a decision's clothes,
        and they now live on `standing`, where they can be recomputed from the
        policy without anyone's judgement being overwritten.
        """
        PENDING = "PENDING", "Pending registration"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"
        DECEASED = "DECEASED", "Deceased"
        CLOSED = "CLOSED", "Closed"

    # Statuses in which a membership still exists and can be worked with. "Live"
    # is NOT "covered": a suspended member is still on the books and can be
    # reinstated, but whether they may CLAIM is a question for the policy,
    # answered by the eligibility engine — never by a status check scattered
    # through the code.
    LIVE_STATUSES = [Status.ACTIVE, Status.SUSPENDED]

    # Statuses from which nothing more is expected of the member, and nothing more
    # is owed to them.
    ENDED_STATUSES = [Status.WITHDRAWN, Status.DECEASED, Status.CLOSED]

    number = models.CharField(max_length=24, unique=True, editable=False,
                              help_text="Permanent membership reference; assigned once, never reused.")
    scheme = models.ForeignKey(BenevolentScheme, on_delete=models.PROTECT,
                               related_name="memberships")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT,
                               related_name="scheme_memberships")
    joined_on = models.DateField(db_index=True,
                                 help_text="The date cover begins counting from (waiting periods run from here).")
    left_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    email = models.EmailField(
        blank=True,
        help_text="Optional. members.Member has no email field at all (the church "
                  "tracks members by name and phone) — this is scoped to the "
                  "benevolent module specifically, for a member who wants email "
                  "notices about their own scheme membership. Blank means email "
                  "notifications simply do not fire for them; SMS to their phone "
                  "still does.")
    notes = models.CharField(max_length=200, blank=True)

    # ---- Registration (Phase 2) --------------------------------------------
    registered_on = models.DateField(
        null=True, blank=True,
        help_text="When the member was formally admitted. Where the policy requires "
                  "registration, cover runs from HERE, not from the enrolment date.")
    registration_fee_paid = models.BooleanField(default=False)
    registration_form_on_file = models.BooleanField(default=False)
    id_document_on_file = models.BooleanField(default=False)
    date_of_birth = models.DateField(
        null=True, blank=True,
        help_text="Only needed where the policy sets a minimum or maximum joining age.")

    # ---- Renewal (Phase 2) --------------------------------------------------
    renewed_until = models.DateField(
        null=True, blank=True,
        help_text="The date this membership's current subscription runs to.")

    # ---- Inactivity (Phase 2) ----------------------------------------------
    inactive_since = models.DateField(null=True, blank=True, editable=False)
    reinstated_on = models.DateField(
        null=True, blank=True,
        help_text="If the member lapsed and came back: when. A reinstatement waiting "
                  "period runs from this date, not from the original joining date.")

    # ---- Household (Phase 3) ------------------------------------------------
    registration_type = models.CharField(
        max_length=10, choices=RegistrationType.choices,
        default=RegistrationType.INDIVIDUAL, db_index=True,
        help_text="An individual enrolment, or one subscription covering a household.")
    household_name = models.CharField(
        max_length=120, blank=True,
        help_text="Under a household registration, the household this enrolment covers "
                  "(e.g. 'the Otieno household').")

    # ---- Standing (Phase 3) — COMPUTED, never hand-set ----------------------
    standing = models.CharField(
        max_length=10, choices=Standing.choices, default=Standing.PENDING,
        db_index=True, editable=False,
        help_text="Where this member stands under the policy. Derived by "
                  "benevolent.services.standing.assess() — a pure function of the "
                  "policy and the facts. This column is a CACHE of that function, so "
                  "recomputing it can never lose information, and a nightly job "
                  "writing here can never overwrite a human's decision (which lives on "
                  "`status`).")
    standing_reason = models.CharField(max_length=200, blank=True, editable=False)
    standing_as_of = models.DateField(null=True, blank=True, editable=False)

    # ---- Death and transfer (Phase 3) --------------------------------------
    died_on = models.DateField(null=True, blank=True)
    transferred_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="transferred_from",
        help_text="Where this membership went, if it was passed to a successor.")
    succeeded_from = models.ForeignKey(
        "members.Member", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
        help_text="The member this enrolment was inherited from, if it was. Their "
                  "joining date is kept, so the years they paid in are not lost by "
                  "the household.")

    enrolled_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="benevolent_enrolments")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["member__name"]
        constraints = [models.UniqueConstraint(fields=["scheme", "member"],
                                               name="uniq_membership_per_scheme")]
        indexes = [models.Index(fields=["scheme", "status"])]

    def __str__(self):
        return f"{self.number} · {self.member.name}"

    def save(self, *args, **kwargs):
        if not self.number:
            year = (self.joined_on or _dt.date.today()).year
            self.number = MembershipSequence.next_number(year, prefix=self.scheme.code)
        super().save(*args, **kwargs)

    @property
    def is_live(self):
        return self.status in self.LIVE_STATUSES

    # ---- contributions (read through the source documents) ---------------
    def contributions_qs(self):
        return self.contributions.all()

    @property
    def contributions_total(self):
        from benevolent.services.contributions import contributions_total
        return contributions_total(membership=self)

    @property
    def contribution_count(self):
        return sum(1 for c in self.contributions.select_related("transaction") if c.effective)

    def days_enrolled(self, on=None):
        on = on or _dt.date.today()
        return max(0, (on - self.cover_from).days)

    # ---- Phase 2 helpers ----------------------------------------------------
    @property
    def cover_from(self):
        """THE date every waiting period counts from — one definition, used by
        the engine and nothing else allowed to re-derive it.

        Three things can move it, in order of precedence:
          * a REINSTATEMENT — a member who lapsed and came back starts their
            waiting period again from the day they returned. (Without this, a
            member could lapse for years, rejoin the week a relative fell ill and
            claim immediately on the strength of a joining date from 2019 — the
            single most obvious way to game a welfare scheme.)
          * formal REGISTRATION, where the policy requires it — cover runs from
            admission, not from the day a name was typed into a list.
          * otherwise, the joining date.
        """
        if self.reinstated_on:
            return self.reinstated_on
        if self.registered_on:
            return self.registered_on
        return self.joined_on

    def last_contribution_date(self):
        from benevolent.services.contributions import contributions_qs
        c = contributions_qs(membership=self).order_by("-transaction__date").first()
        return c.date if c else None

    def months_since_contribution(self, as_of=None):
        """Whole months since the last contribution — or since cover began, for a
        member who has never contributed at all (who is otherwise invisible to an
        inactivity rule, which is precisely the wrong answer)."""
        as_of = as_of or _dt.date.today()
        last = self.last_contribution_date() or self.cover_from
        return max(0, (as_of.year - last.year) * 12 + (as_of.month - last.month))

    def renewal_due_on(self, policy=None, as_of=None):
        """When this membership's subscription next falls due, or None if the
        policy has no renewal rule. Every membership renews in the same month —
        that is how a church actually runs a subscription year, rather than
        chasing 200 individual anniversaries."""
        as_of = as_of or _dt.date.today()
        policy = policy or self.scheme.policy_on(as_of)
        if policy is None or not policy.renewal_required:
            return None
        if policy.renewal_period == SchemePolicy.RenewalPeriod.NONE:
            return None
        step = 2 if policy.renewal_period == SchemePolicy.RenewalPeriod.BIENNIAL else 1
        base = self.renewed_until
        if base:
            return base
        year = self.cover_from.year
        due = _dt.date(year, policy.renewal_month or 1, 1)
        if due < self.cover_from:
            due = _dt.date(year + step, policy.renewal_month or 1, 1)
        return due

    def renewal_overdue(self, policy=None, as_of=None):
        as_of = as_of or _dt.date.today()
        policy = policy or self.scheme.policy_on(as_of)
        due = self.renewal_due_on(policy, as_of)
        if due is None:
            return False
        grace = _dt.timedelta(days=(policy.renewal_grace_days or 0))
        return as_of > (due + grace)


class SchemeDependant(models.Model):
    """A person covered under a member's enrolment (spouse, child, parent). A
    bereavement or medical case is very often claimed FOR a dependant, so who is
    covered has to be on record before the event, not asserted after it."""

    class Relationship(models.TextChoices):
        SPOUSE = "SPOUSE", "Spouse"
        CHILD = "CHILD", "Child"
        PARENT = "PARENT", "Parent"
        SIBLING = "SIBLING", "Sibling"
        OTHER = "OTHER", "Other dependant"

    membership = models.ForeignKey(SchemeMembership, on_delete=models.CASCADE,
                                   related_name="dependants")
    # A dependant who is themselves on the church roll is LINKED, not typed in a
    # second time. This is the whole "extend Members, do not duplicate it"
    # instruction made concrete: a spouse who is a church member has ONE record,
    # and their name, phone and status cannot drift between the roll and the
    # scheme. Where the dependant is not a church member (a young child, an
    # elderly parent in the village), `name` carries them, and nothing is lost.
    member = models.ForeignKey(
        "members.Member", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="benevolent_dependencies",
        help_text="Link this dependant to their church-member record where they have "
                  "one, so their details are never kept in two places.")
    name = models.CharField(
        max_length=120, blank=True,
        help_text="Only needed for a dependant who is not on the church roll.")
    relationship = models.CharField(max_length=8, choices=Relationship.choices)
    phone = models.CharField(
        max_length=20, blank=True, db_index=True,
        help_text="A spouse or grown child very often pays the member's dues from "
                  "their OWN phone. Recording the number here lets the allocator "
                  "recognise that money instead of dropping it into an unmatched "
                  "queue for a treasurer to puzzle over.")
    date_of_birth = models.DateField(null=True, blank=True)
    registered_on = models.DateField(default=_dt.date.today,
                                     help_text="When this dependant was registered on the scheme.")
    active = models.BooleanField(default=True, db_index=True)
    removed_on = models.DateField(null=True, blank=True)
    died_on = models.DateField(
        null=True, blank=True,
        help_text="Set only when this dependant is recorded as deceased — distinct "
                  "from removed_on, which is set for ANY reason a dependant leaves "
                  "cover (moved away, aged out, a correction). A case can still be "
                  "raised for them after this; see BenevolentCase.dependant.")
    notes = models.CharField(max_length=200, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["relationship", "name"]

    def __str__(self):
        return f"{self.display_name} ({self.get_relationship_display()})"

    @property
    def display_name(self):
        """The member registry wins where it knows them — one name, one place."""
        return (self.member.name if self.member_id else self.name) or "(unnamed)"

    @property
    def is_spouse(self):
        return self.relationship == self.Relationship.SPOUSE

    def clean(self):
        if not self.member_id and not (self.name or "").strip():
            raise ValidationError(
                "Give the dependant a name, or link them to their church-member record.")

    def covered_on(self, date):
        """Was this dependant on record, and still covered, when the event happened?
        Registering someone after the fact is the oldest trick there is."""
        if self.registered_on and self.registered_on > date:
            return False
        if self.removed_on and self.removed_on <= date:
            return False
        return True


# ---------------------------------------------------------------------------
# Money in — the scheme-side index over giving.Transaction
# ---------------------------------------------------------------------------

class BenevolentContribution(models.Model):
    """The scheme-side index over a contribution receipt.

    The `giving.Transaction` remains authoritative for the money; this row records
    only what the receipt cannot know — which enrolment it settles, which dues
    period it covers, which case's levy it answers, and WHAT KIND of money it is.
    """

    class Kind(models.TextChoices):
        """What KIND of money came in.

        Not a label. Only DUES settle dues, so misclassifying a levy or a
        registration fee would let it silently clear a member's own arrears — and
        the scheme's arrears book would go quietly and permanently wrong. Every
        route into `record_contribution` therefore says what kind of money it is,
        and where it does not, the engine infers it from the evidence rather than
        guessing a default.
        """
        DUES = "DUES", "Periodic dues"
        LEVY = "LEVY", "Per-case levy"
        REGISTRATION = "REGISTRATION", "Registration fee"
        RENEWAL = "RENEWAL", "Renewal fee"
        PENALTY = "PENALTY", "Payment of a penalty"
        VOLUNTARY = "VOLUNTARY", "Voluntary contribution"
        DONATION = "DONATION", "Donation or gift (from anyone)"

    # The kinds that actually settle a member's periodic dues. Everything else is
    # money the member has given the scheme WITHOUT it being a subscription — and
    # `arrears_for()` must not treat it as one.
    SETTLES_DUES = [Kind.DUES]

    # The kinds that are a member meeting an obligation (as opposed to a gift).
    OBLIGATIONS = [Kind.DUES, Kind.LEVY, Kind.REGISTRATION, Kind.RENEWAL, Kind.PENALTY]

    kind = models.CharField(
        max_length=12, choices=Kind.choices, default=Kind.DUES, db_index=True,
        help_text="What kind of money this is. It matters: only DUES count against a "
                  "member's dues, so a levy paid towards someone else's bereavement — or "
                  "a registration fee — can never silently clear a member's own arrears.")

    # Where the money came in from an unattended channel and the allocator decided
    # who it belonged to, this records how sure it was — so a treasurer reading the
    # member's statement can see which lines a machine attributed and which a human
    # did. A confidently-wrong allocation is the failure mode worth being able to
    # audit for.
    allocated_automatically = models.BooleanField(default=False, db_index=True)
    allocation_confidence = models.PositiveSmallIntegerField(default=0)

    scheme = models.ForeignKey(BenevolentScheme, on_delete=models.PROTECT,
                               related_name="contributions")
    membership = models.ForeignKey(
        SchemeMembership, null=True, blank=True, on_delete=models.PROTECT,
        related_name="contributions",
        help_text="Blank for a general donation into the scheme by someone who is not enrolled.")
    transaction = models.OneToOneField(
        "giving.Transaction", on_delete=models.PROTECT, related_name="benevolent_contribution",
        help_text="The receipt that carries the money. Authoritative for amount and date.")
    period_label = models.CharField(
        max_length=10, blank=True, db_index=True,
        help_text="The dues period this settles, e.g. '2026-07' (monthly) or '2026' (annual).")
    case = models.ForeignKey(
        "BenevolentCase", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="levy_contributions",
        help_text="For a per-case levy: the case the levy was raised for.")
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="benevolent_contributions_recorded")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-transaction__date", "-id"]
        indexes = [models.Index(fields=["scheme", "period_label"])]

    def __str__(self):
        return f"{self.scheme.code} contribution {self.amount}"

    def clean(self):
        if self.membership_id and self.membership.scheme_id != self.scheme_id:
            raise ValidationError("The membership belongs to a different scheme.")

    @property
    def amount(self):
        t = self.transaction
        return t.amount if t else Decimal(0)

    @property
    def date(self):
        t = self.transaction
        return t.date if t else None

    @property
    def effective(self):
        """Whether the underlying receipt still counts. A reversed or unconfirmed
        receipt is not a contribution — and needs no separate correction here."""
        t = self.transaction
        return bool(t and t.confirmed and not t.is_reversed and not t.is_reversal)


# ---------------------------------------------------------------------------
# The case — a claim on the scheme
# ---------------------------------------------------------------------------

class BenevolentCase(models.Model):
    """A claim: an event happened to a member (or their dependant), the policy
    in force says what it is worth, and the scheme pays a benefit.

    The case freezes the policy version and the eligibility evaluation it was
    decided under. That, not the mutable policy row, is the audit record.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        ASSESSED = "ASSESSED", "Assessed"
        APPROVED = "APPROVED", "Approved for payment"
        PARTLY_PAID = "PARTLY_PAID", "Partly paid"
        PAID = "PAID", "Paid"
        CLOSED = "CLOSED", "Closed"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    OPEN_STATUSES = [Status.DRAFT, Status.SUBMITTED, Status.ASSESSED,
                     Status.APPROVED, Status.PARTLY_PAID]
    EDITABLE_STATUSES = [Status.DRAFT, Status.SUBMITTED]
    FINAL_STATUSES = [Status.PAID, Status.CLOSED, Status.REJECTED, Status.CANCELLED]

    number = models.CharField(max_length=24, unique=True, editable=False,
                              help_text="Permanent case reference; assigned once, never reused.")
    scheme = models.ForeignKey(BenevolentScheme, on_delete=models.PROTECT,
                               related_name="cases")
    membership = models.ForeignKey(
        SchemeMembership, null=True, blank=True, on_delete=models.PROTECT,
        related_name="cases",
        help_text="The claiming enrolment. Blank only where the policy permits "
                  "non-member claims (e.g. a benevolence-to-the-community scheme).")
    event_type = models.ForeignKey(BenevolentEventType, on_delete=models.PROTECT,
                                   related_name="cases")

    # Who the benefit is for. Either a registered dependant, or a named person
    # (which covers a non-member claim, where the policy allows one).
    dependant = models.ForeignKey(SchemeDependant, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="cases")
    beneficiary_name = models.CharField(
        max_length=120, blank=True,
        help_text="Who the benefit is for, if not the member themselves.")

    event_date = models.DateField(db_index=True, help_text="When the event occurred.")
    reported_date = models.DateField(default=_dt.date.today, db_index=True,
                                     help_text="When the church was told (drives the claim window).")
    description = models.TextField(blank=True)

    claimed_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="What was requested / the assessed cost. Used by percentage-mode policies.")
    assessed_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="The entitlement the policy engine computed at assessment. Never edited by hand.")
    approved_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="What the approver authorised. This — not the assessment — is what may be paid.")

    # ---- Funding target (Phase 5) -------------------------------------------
    # Distinct from assessed_amount/approved_amount on purpose. Under a POOLED
    # or PER_MEMBER_MULTIPLE policy the entitlement itself MOVES as levy money
    # comes in — but a family, a committee, or a treasurer often still needs an
    # explicit goal to work towards and show progress against ("we are aiming
    # for 50,000") that is independent of, and usually set well before, any
    # policy computation. It is never used BY the eligibility engine — the
    # policy alone still decides what is owed — it is purely a fundraising
    # goal the case tracks itself against.
    funding_target = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="The amount this case is aiming to raise or receive. Optional, "
                  "and separate from the assessed/approved benefit — a goal to "
                  "track progress against, not a rule the policy enforces.")
    funding_target_set_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    funding_target_set_at = models.DateTimeField(null=True, blank=True)

    # ---- The bereaved member's own contribution, where the policy leaves it
    # to the committee (SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES)
    bereaved_levy_waived = models.BooleanField(
        null=True, blank=True, default=None,
        help_text="Only meaningful under a COMMITTEE_DECIDES bereaved policy. "
                  "None = not yet decided (the member is left off the levy "
                  "roster while it is pending — nobody chases a grieving family "
                  "on the strength of a rule nobody has actually applied yet). "
                  "True = waived for this case. False = they contribute as "
                  "normal.")
    bereaved_levy_decision_reason = models.TextField(blank=True)
    bereaved_levy_decided_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    bereaved_levy_decided_at = models.DateTimeField(null=True, blank=True)

    # ---- the frozen decision basis (immutability) ------------------------
    policy = models.ForeignKey(
        SchemePolicy, null=True, blank=True, on_delete=models.PROTECT, related_name="cases",
        help_text="The exact policy version this case was assessed under.")
    policy_snapshot = models.JSONField(
        default=dict, blank=True,
        help_text="The policy's terms, frozen at assessment. The decision remains "
                  "reproducible even if the policy row were later altered.")
    eligibility_snapshot = models.JSONField(
        default=dict, blank=True,
        help_text="The full eligibility evaluation (every check, passed or failed) "
                  "as it stood when the case was assessed.")

    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.DRAFT, db_index=True)
    override_reason = models.TextField(
        blank=True,
        help_text="Required when a case is approved despite failing an eligibility check. "
                  "The reason is part of the permanent record.")
    rejection_reason = models.TextField(blank=True)

    raised_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="benevolent_cases_raised")
    submitted_at = models.DateTimeField(null=True, blank=True)
    assessed_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="benevolent_cases_assessed")
    assessed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="benevolent_cases_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="benevolent_cases_rejected")
    closed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-event_date", "-id"]
        indexes = [models.Index(fields=["scheme", "status"]),
                   models.Index(fields=["status", "event_date"])]

    def __str__(self):
        return f"{self.number} · {self.beneficiary_display}"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = CaseSequence.next_number(
                (self.event_date or _dt.date.today()).year,
                prefix=f"{self.scheme.code}C")
        super().save(*args, **kwargs)

    def clean(self):
        if self.event_date and self.reported_date and self.reported_date < self.event_date:
            raise ValidationError("A case cannot be reported before the event happened.")
        if self.membership_id and self.membership.scheme_id != self.scheme_id:
            raise ValidationError("That membership belongs to a different scheme.")
        if self.event_type_id and self.event_type.scheme_id != self.scheme_id:
            raise ValidationError("That event type belongs to a different scheme.")
        if self.dependant_id and self.membership_id \
                and self.dependant.membership_id != self.membership_id:
            raise ValidationError("That dependant is registered under a different membership.")

    # ---- display ---------------------------------------------------------
    @property
    def beneficiary_display(self):
        if self.dependant_id:
            return f"{self.dependant.name} ({self.dependant.get_relationship_display()})"
        if self.beneficiary_name:
            return self.beneficiary_name
        if self.membership_id:
            return self.membership.member.name
        return "—"

    @property
    def claimant_display(self):
        return self.membership.member.name if self.membership_id else (self.beneficiary_name or "—")

    # ---- money (computed from the payout documents) ----------------------
    @property
    def paid_total(self):
        """What has actually left the fund — only vouchers that count (approved
        or paid), which is exactly the condition the fund balance itself uses."""
        return sum((p.amount for p in self.payouts.select_related("expense") if p.effective),
                   Decimal(0))

    @property
    def committed_total(self):
        """Vouchers raised and still live, but not yet approved. The money has
        not moved, but the authorisation is spoken for."""
        return sum((p.amount for p in self.payouts.select_related("expense")
                    if p.expense_id and not p.effective and not p.is_rejected), Decimal(0))

    @property
    def outstanding(self):
        """Approved benefit not yet paid out — what the scheme still OWES the
        beneficiary. This is the commitment figure the reports show."""
        if self.approved_amount is None:
            return Decimal(0)
        return max(Decimal(0), self.approved_amount - self.paid_total)

    @property
    def available_to_voucher(self):
        """How much of the approved benefit may still be put on a NEW voucher.

        Distinct from `outstanding` on purpose, and the distinction is a control:
        a pending voucher has not moved money (so it is not in `paid_total`), but
        it has already claimed its share of the authorisation. Without this, two
        or three pending vouchers could each be raised for the full approved
        amount, and the case would overpay the moment they were all approved.
        A rejected voucher correctly releases its amount again.
        """
        if self.approved_amount is None:
            return Decimal(0)
        return max(Decimal(0),
                   self.approved_amount - self.paid_total - self.committed_total)

    @property
    def funding_collected(self):
        """Money actually raised FOR this case so far — every contribution
        (almost always a levy) tagged to it. Reads the contribution index, the
        same figure `services.contributions.levy_collected` reports, so the
        funding-progress bar and the levy round can never disagree."""
        return sum((c.amount for c in self.levy_contributions.select_related("transaction")
                    if c.effective), Decimal(0))

    @property
    def funding_progress_percent(self):
        """0-100, or None where there is no target to measure against — a case
        under a kitty-funded policy simply has nothing to show a bar for."""
        if not self.funding_target or self.funding_target <= 0:
            return None
        pct = (self.funding_collected / self.funding_target) * 100
        return min(Decimal(100), pct)

    @property
    def funding_fully_raised(self):
        return bool(self.funding_target) and self.funding_collected >= self.funding_target

    @property
    def bereaved_levy_decision_pending(self):
        """True only where the policy actually asks the committee, and they
        have not yet answered."""
        policy = self.policy
        if policy is None:
            return False
        return (policy.bereaved_contribution_policy ==
                SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES
                and self.bereaved_levy_waived is None)

    @property
    def is_editable(self):
        return self.status in self.EDITABLE_STATUSES

    @property
    def is_final(self):
        return self.status in self.FINAL_STATUSES

    @property
    def eligible(self):
        """Did the frozen evaluation pass? None if never assessed."""
        snap = self.eligibility_snapshot or {}
        return snap.get("eligible") if snap else None

    @property
    def failed_checks(self):
        return [c for c in (self.eligibility_snapshot or {}).get("checks", [])
                if not c.get("passed")]

    def refresh_status(self, save=True):
        """Derive the payment status from the payout documents — the same
        'documents are authoritative' rule the loans module uses. Never demotes
        a case out of a final decision (REJECTED/CANCELLED/CLOSED stay put)."""
        if self.status in (self.Status.REJECTED, self.Status.CANCELLED,
                           self.Status.CLOSED, self.Status.DRAFT,
                           self.Status.SUBMITTED, self.Status.ASSESSED):
            return self.status
        paid = self.paid_total
        approved = self.approved_amount or Decimal(0)
        if paid <= 0:
            new = self.Status.APPROVED
        elif paid < approved:
            new = self.Status.PARTLY_PAID
        else:
            new = self.Status.PAID
        if new != self.status:
            self.status = new
            if save:
                self.save(update_fields=["status"])
        return self.status


class BenevolentPayout(models.Model):
    """Indexes ONE cashbook.Expense as a benefit payment on a case.

    The Expense is authoritative: it carries the money, the approval, the
    voucher, the payment method and the ledger posting (DR Benevolence / CR
    Cash). A case may have several payouts (staged payments, or a benefit split
    between a funeral home and the family), so this is a plain FK, not a
    OneToOne on the case.
    """

    case = models.ForeignKey(BenevolentCase, on_delete=models.PROTECT, related_name="payouts")
    expense = models.OneToOneField(
        "cashbook.Expense", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="benevolent_payout",
        help_text="The voucher that carries the money. Authoritative for amount, "
                  "approval and payment.")
    payee_name = models.CharField(max_length=120, blank=True,
                                  help_text="Who was actually paid (may be a third party).")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="benevolent_payouts_created")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.case.number} payout {self.amount}"

    @property
    def amount(self):
        e = self.expense
        return e.amount if e else Decimal(0)

    @property
    def date(self):
        e = self.expense
        return e.date if e else None

    @property
    def status(self):
        e = self.expense
        return e.get_status_display() if e else "—"

    @property
    def effective(self):
        """Whether the voucher counts against the scheme's fund. Mirrors exactly
        the condition the balance engine uses (APPROVED or PAID), so a payout is
        never counted here that the fund balance doesn't also feel."""
        from cashbook.models import Expense
        e = self.expense
        return bool(e and e.status in (Expense.Status.APPROVED, Expense.Status.PAID))

    @property
    def is_rejected(self):
        from cashbook.models import Expense
        e = self.expense
        return bool(e and e.status == Expense.Status.REJECTED)


def case_attachment_path(instance, filename):
    return f"benevolent/cases/{instance.case_id}/{filename}"


class CaseAttachment(models.Model):
    """Supporting evidence for a case (burial permit, medical report, invoice)."""
    case = models.ForeignKey(BenevolentCase, on_delete=models.CASCADE,
                             related_name="attachments")
    file = models.FileField(upload_to=case_attachment_path)
    document_type = models.CharField(
        max_length=120, blank=True,
        help_text="Matched against the event type's required_documents checklist, "
                  "where it has one. Free text otherwise.")
    label = models.CharField(max_length=120, blank=True)
    uploaded_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.label or self.file.name


# ---------------------------------------------------------------------------
# Phase 2 — Constitution, Settings & Policy Engine
#
# Kept in their own module for readability (this file is already long), imported
# here so Django's app registry picks them up as ordinary `benevolent` models.
# See benevolent/models_config.py for the settings-vs-policy design note.
# ---------------------------------------------------------------------------
from benevolent.models_config import (  # noqa: E402,F401
    BenevolentSettings, CaseApproval, PolicyProfile, SchemeNominee)
from benevolent.models_contrib import (  # noqa: E402,F401
    ContributionIntake, ContributionRefund, ContributionRule, MemberAdjustment)
from benevolent.models_case import CaseEvent  # noqa: E402,F401
from benevolent.models_committee import CommitteeMember  # noqa: E402,F401
from benevolent.models_notify import (  # noqa: E402,F401
    BenevolentNotification, NotificationEvent, NotificationTemplate)


# The public application form (Round 4). See models_public.
from benevolent.models_public import (  # noqa: E402,F401
    ApplicationDependant, BenevolentApplication)
