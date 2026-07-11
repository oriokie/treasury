"""Phase 2 — Constitution, Settings & Policy Engine.

The one architectural line that governs this whole phase
--------------------------------------------------------
Every configurable thing in the benevolent module goes to one of two homes, and
the test for which is a single question:

    *Does it decide an outcome?*

  YES → it is a RULE. It lives on `SchemePolicy` (Phase 1's model, extended
        here), which is VERSIONED and becomes IMMUTABLE the instant a case is
        decided under it. Registration requirements, fees, renewals,
        contribution models, benefit calculations, committee approvals, bereaved
        rules, inactivity rules, household policies and inheritance rules are all
        rules: change one and a claim that would have been paid might now be
        refused. They therefore CANNOT be free-form settings, because editing a
        setting would silently rewrite the basis of decisions already made.

  NO  → it is a SETTING. It lives on `BenevolentSettings` (this module), which is
        a plain, freely-editable singleton. Accounting mappings, notification
        preferences, automation cadence and defaults-for-new-schemes steer how
        the module *operates*; none of them can change whether a past claim
        qualified or what it was worth.

That line is what makes the brief's two requirements — "all church-specific
behaviour must be driven through configuration" and "policy changes are
version-controlled and do not modify historical transactions" — hold at the same
time, rather than trading off against each other.

Accounting mappings sit on the SETTINGS side deliberately, and it is worth saying
why: every posted document (a `giving.Transaction`, a `cashbook.Expense`) stores
its own fund and category at the moment it is written. Re-pointing a mapping
therefore steers *future* postings only and is physically incapable of rewriting a
historical one. The ledger's history is safe by construction, not by policy.
"""
import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords


# ---------------------------------------------------------------------------
# Module settings — the freely-editable side of the line
# ---------------------------------------------------------------------------

class BenevolentSettings(models.Model):
    """One editable row of module configuration. Use BenevolentSettings.get().

    Deliberately SEPARATE from core.SiteConfig. The benevolent module is a
    self-contained domain with its own administrators (a welfare secretary may
    run it without ever touching church-wide settings), so its configuration gets
    its own row, its own page and its own right — while inheriting the
    application's theme, layout, tab framework and permission model wholesale.

    Nothing here is versioned, because nothing here can change the outcome of a
    decision already made. Anything that could belongs on SchemePolicy.
    """

    # ---- Accounting mappings ------------------------------------------------
    # These steer where FUTURE postings land. They cannot rewrite a historical
    # one: every Transaction and Expense stores its own fund and category when
    # written, so re-pointing a mapping is incapable of touching the past.
    default_benefit_category = models.CharField(
        max_length=14, default="BENEVOLENCE",
        help_text="The expense category benefit payments are charged to. Must be a "
                  "category the cash book knows (built-in or custom).")
    registration_fee_fund = models.ForeignKey(
        "departments.Department", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
        help_text="Where registration and renewal fees are receipted. Leave blank to "
                  "receipt them into the scheme's own fund alongside contributions.")
    separate_levy_tracking = models.BooleanField(
        default=True,
        help_text="Track per-case levies against the case that raised them, so a "
                  "levy round can be reconciled to the benefit it funded.")

    # ---- Notification preferences -------------------------------------------
    notify_on_case_submitted = models.BooleanField(default=True)
    notify_on_case_approved = models.BooleanField(default=True)
    notify_on_case_rejected = models.BooleanField(default=True)
    notify_on_payout_raised = models.BooleanField(default=True)
    notify_on_levy_raised = models.BooleanField(default=True)
    notify_committee_on_pending_vote = models.BooleanField(
        default=True,
        help_text="Tell committee members when a case is waiting for their decision.")
    notify_member_on_enrolment = models.BooleanField(default=False)
    notify_member_on_benefit_paid = models.BooleanField(default=False)
    notify_member_on_arrears = models.BooleanField(default=False)

    class Channel(models.TextChoices):
        IN_APP = "IN_APP", "In-app only"
        IN_APP_EMAIL = "IN_APP_EMAIL", "In-app and email"
        IN_APP_SMS = "IN_APP_SMS", "In-app and SMS"
        ALL = "ALL", "In-app, email and SMS"

    staff_channel = models.CharField(
        max_length=12, choices=Channel.choices, default=Channel.IN_APP,
        help_text="How treasury staff and committee members are told. Email and SMS "
                  "are only ever sent if they are configured and enabled in the main "
                  "system settings — this never overrides that.")
    member_channel = models.CharField(
        max_length=12, choices=Channel.choices, default=Channel.IN_APP_SMS,
        help_text="How members are told (where member notices are switched on above).")

    # ---- Automation ---------------------------------------------------------
    automation_enabled = models.BooleanField(
        default=False,
        help_text="Master switch. With this off, nothing below runs, whatever it says.")
    auto_refresh_arrears = models.BooleanField(
        default=True, help_text="Mark members lapsed when they fall behind, and reinstate "
                                "them when they catch up.")
    auto_flag_inactive = models.BooleanField(
        default=True, help_text="Apply the policy's inactivity rule to members who have "
                                "stopped contributing.")
    auto_lapse_unrenewed = models.BooleanField(
        default=True, help_text="Apply the policy's renewal rule when a membership's "
                                "renewal falls due and is not paid within the grace period.")
    arrears_reminder_days = models.PositiveIntegerField(
        default=0,
        help_text="Remind a member this many days after they fall into arrears. "
                  "0 = never remind.")
    renewal_reminder_days = models.PositiveIntegerField(
        default=30,
        help_text="Remind a member this many days before their renewal falls due. "
                  "0 = never remind.")
    automation_last_run = models.DateTimeField(null=True, blank=True, editable=False)
    automation_last_summary = models.CharField(max_length=255, blank=True, editable=False)

    # ---- Intelligent allocation (Phase 4) -----------------------------------
    auto_allocate = models.BooleanField(
        default=True,
        help_text="Let the allocator attach a receipt to a member by itself when it is "
                  "confident enough. With this off, every receipt goes to the review "
                  "queue — which is slower, and which some treasurers will rightly "
                  "prefer for their first month.")
    auto_allocate_threshold = models.PositiveSmallIntegerField(
        default=85,
        help_text="Confidence (0–100) at or above which a receipt is attached without a "
                  "human. Set it high: a confidently WRONG allocation is worse than an "
                  "honest queue, because nobody goes looking for it.")
    review_threshold = models.PositiveSmallIntegerField(
        default=40,
        help_text="Confidence at or above which a receipt goes to the REVIEW queue with "
                  "suggestions. Below this it goes to the UNMATCHED queue with none — "
                  "which is more honest than a bad guess.")
    fuzzy_name_threshold = models.PositiveSmallIntegerField(
        default=82,
        help_text="How close a name has to be (0–100) before it counts as a match at all. "
                  "Kenyan bank narrations abbreviate and reorder names constantly, so some "
                  "fuzziness is essential — but two brothers share a surname, so it must "
                  "never be the ONLY evidence.")
    duplicate_window_days = models.PositiveSmallIntegerField(
        default=3,
        help_text="Flag a receipt as a possible duplicate if the same member paid the same "
                  "amount to the same scheme within this many days. 0 = never flag.")
    learn_allocation_rules = models.BooleanField(
        default=True,
        help_text="Propose a narration rule after a treasurer has allocated the same "
                  "unrecognised narration by hand a few times.")

    # ---- Defaults for new schemes ------------------------------------------
    default_profile = models.ForeignKey(
        "PolicyProfile", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
        help_text="The policy profile a new scheme starts from. This is a DEFAULT, not "
                  "a live rule: it seeds a draft policy, which then has to be published "
                  "before it governs anything.")
    require_wizard_for_new_schemes = models.BooleanField(
        default=False,
        help_text="Send anyone creating a scheme through the Constitution Wizard rather "
                  "than a blank policy form.")

    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "Benevolent settings"
        verbose_name_plural = "Benevolent settings"

    def __str__(self):
        return "Benevolent settings"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    # ---- channel helpers (one place decides who hears what, and how) --------
    def staff_email(self):
        return self.staff_channel in (self.Channel.IN_APP_EMAIL, self.Channel.ALL)

    def staff_sms(self):
        return self.staff_channel in (self.Channel.IN_APP_SMS, self.Channel.ALL)

    def member_email(self):
        return self.member_channel in (self.Channel.IN_APP_EMAIL, self.Channel.ALL)

    def member_sms(self):
        return self.member_channel in (self.Channel.IN_APP_SMS, self.Channel.ALL)

    def wants(self, event):
        """Whether a given module event should notify anyone at all."""
        return bool(getattr(self, f"notify_on_{event}", False))


# ---------------------------------------------------------------------------
# Policy profiles — reusable constitutions
# ---------------------------------------------------------------------------

class PolicyProfile(models.Model):
    """A named, reusable bundle of policy settings — a constitution template.

    Churches do not invent welfare schemes from nothing; they run one of a small
    number of well-known shapes (monthly dues with a fixed benefit; a harambee
    levy raised per bereavement; a hybrid of the two; a medical scheme paying a
    percentage of cost). A profile captures one of those shapes once, so the next
    church configures a scheme by choosing and adjusting rather than by answering
    forty questions from scratch.

    A profile is NOT a live policy and governs nothing. Applying it CREATES A
    DRAFT `SchemePolicy`, which still has to be published before it decides
    anything — so a profile can be edited freely without any of the immutability
    concerns that (rightly) surround a policy version.
    """

    name = models.CharField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    kind = models.CharField(
        max_length=12, default="BENEVOLENT",
        help_text="The scheme kind this profile suits (a label, as on the scheme).")
    config = models.JSONField(
        default=dict,
        help_text="Policy field values, keyed exactly as SchemePolicy.RULE_FIELDS.")
    benefit_lines = models.JSONField(
        default=list, blank=True,
        help_text="Suggested benefit schedule: [{event, code, amount, cap, max_per_year}].")
    builtin = models.BooleanField(
        default=False, editable=False,
        help_text="Shipped with the system. May be copied and adjusted freely, but "
                  "is protected from deletion so the library always has a starting point.")
    created_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-builtin", "name"]

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        if self.builtin:
            raise ValidationError(
                f"'{self.name}' is a built-in profile and cannot be deleted. Copy it and "
                f"adjust the copy instead — the library must always have a starting point.")
        return super().delete(*args, **kwargs)

    def summary(self):
        """A one-line human description, derived from the config rather than
        stored — so it can never fall out of step with what the profile does."""
        c = self.config or {}
        bits = []
        cm = c.get("contribution_mode")
        if cm == "FIXED_PERIODIC":
            bits.append(f"{c.get('contribution_amount', 0)} "
                        f"{(c.get('contribution_frequency') or 'monthly').lower()} dues")
        elif cm == "PER_CASE_LEVY":
            bits.append(f"levy of {c.get('levy_amount') or c.get('contribution_amount', 0)} per case")
        elif cm == "HYBRID":
            bits.append(f"{c.get('contribution_amount', 0)} dues + "
                        f"{c.get('levy_amount', 0)} levy per case")
        elif cm == "VOLUNTARY":
            bits.append("voluntary giving")
        bm = c.get("benefit_mode")
        labels = {"FIXED": "one fixed benefit", "SCHEDULE": "benefit schedule",
                  "PERCENTAGE": f"{c.get('benefit_percent', 0)}% of cost",
                  "DISCRETIONARY": "discretionary within a cap",
                  "POOLED": "pays out what the levy collects",
                  "PER_MEMBER_MULTIPLE": "levy × the membership"}
        if bm in labels:
            bits.append(labels[bm])
        if c.get("waiting_period_days"):
            bits.append(f"{c['waiting_period_days']}-day wait")
        return " · ".join(bits) or "No rules set"


# ---------------------------------------------------------------------------
# Committee approval — a decision made by a body, not a person
# ---------------------------------------------------------------------------

class CaseApproval(models.Model):
    """One committee member's decision on one case.

    Where a policy requires committee approval, a benefit is not authorised by an
    individual at all: it is authorised when a quorum of recorded decisions is
    reached. Each vote is its own row, with its own author and timestamp, so the
    minute of the decision is reconstructable — which is exactly what a church
    board or an auditor will ask for.
    """

    class Decision(models.TextChoices):
        APPROVE = "APPROVE", "Approve"
        REJECT = "REJECT", "Reject"
        ABSTAIN = "ABSTAIN", "Abstain"

    case = models.ForeignKey("BenevolentCase", on_delete=models.CASCADE,
                             related_name="committee_approvals")
    user = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                             related_name="benevolent_votes")
    decision = models.CharField(max_length=8, choices=Decision.choices)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="The amount this member would approve, if they wish to differ from "
                  "the assessed entitlement.")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["case", "user"],
                                               name="uniq_committee_vote_per_case")]

    def __str__(self):
        return f"{self.user} {self.get_decision_display()} on {self.case.number}"


# ---------------------------------------------------------------------------
# Inheritance — who receives a benefit
# ---------------------------------------------------------------------------

class SchemeNominee(models.Model):
    """Who a member's benefit is paid to, and in what share.

    Recorded BEFORE the event, never asserted after it — the same discipline as
    registered dependants. Where a policy's inheritance rule is NOMINEE, a case on
    the member's own death pays these people, in these shares; a scheme with no
    nominee on file for such a member is a gap the engine will report rather than
    guess about.
    """

    membership = models.ForeignKey("SchemeMembership", on_delete=models.CASCADE,
                                   related_name="nominees")
    name = models.CharField(max_length=120)
    relationship = models.CharField(max_length=40, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    national_id = models.CharField(max_length=20, blank=True)
    share_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("100"),
        help_text="This nominee's share of the benefit. The shares across all of a "
                  "member's nominees must total 100%.")
    is_successor = models.BooleanField(
        default=False,
        help_text="Where the policy allows the membership itself to be inherited, this "
                  "is the person who takes it over — keeping the original joining date, "
                  "so no new waiting period is served.")
    active = models.BooleanField(default=True, db_index=True)
    recorded_on = models.DateField(default=_dt.date.today)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-share_percent", "name"]

    def __str__(self):
        return f"{self.name} ({self.share_percent}%)"

    def clean(self):
        if self.share_percent is None or not (0 < self.share_percent <= 100):
            raise ValidationError("A nominee's share must be more than 0% and at most 100%.")
        if self.membership_id:
            others = (SchemeNominee.objects
                      .filter(membership_id=self.membership_id, active=True)
                      .exclude(pk=self.pk)
                      .aggregate(t=models.Sum("share_percent"))["t"] or Decimal(0))
            if others + (self.share_percent or Decimal(0)) > Decimal("100"):
                raise ValidationError(
                    f"That would take this member's nominated shares to "
                    f"{others + self.share_percent}%. The shares must total 100% or less.")
