"""Phase 3 — Member Registry, Households & Standing.

Two axes, not one
-----------------
Phases 1–2 kept a single `SchemeMembership.status`, and it was quietly carrying
two incompatible jobs:

  * decisions a HUMAN makes    — pending, active, suspended, withdrawn
  * facts about how a member is DOING — lapsed, expired, inactive

Automation therefore wrote into the same field a treasurer wrote into, and the
only thing keeping them apart was a careful allowlist. That works until it
doesn't. Worse, it made a derived fact *look* like a decision: a membership marked
LAPSED told you nothing about whether a person had chosen that or a nightly job
had inferred it.

Phase 3 splits them, and the whole of this module follows from the split:

  ┌──────────────────────────┬──────────────────────────────────────────────┐
  │ `status` — the LIFECYCLE │ `standing` — the STANDING                    │
  ├──────────────────────────┼──────────────────────────────────────────────┤
  │ A human decides it.      │ A pure function computes it, from the policy  │
  │ Never automated.         │ and the facts. Never hand-set.               │
  │ Pending · Active ·       │ Good standing · Exempt · Grace period ·       │
  │ Suspended · Withdrawn ·  │ Arrears · Inactive — plus the lifecycle       │
  │ Deceased · Closed        │ states, which dominate when they apply.       │
  └──────────────────────────┴──────────────────────────────────────────────┘

`standing` is a CACHE of a pure function (`services/standing.assess`). Recomputing
it can never lose information, which is exactly what makes it safe for a nightly
job to touch: automation now writes only to the derived axis, and is structurally
incapable of overriding a treasurer's decision. It is no longer prevented by an
allowlist; it is prevented by there being nowhere for it to write.

Extending Members, not duplicating it
-------------------------------------
`members.Member` remains the ONE record of a person. A `SchemeMembership` is an
enrolment, and a `SchemeDependant` now carries an optional FK to `members.Member`,
so a dependant who is themselves on the church roll is LINKED rather than typed in
a second time. A household is a registration type on the membership — not a
parallel person-database with its own names and phone numbers to drift out of step.
"""
import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords


class Standing(models.TextChoices):
    """Where a member stands — the one word that answers it.

    Computed, never hand-set. The first four are LIFECYCLE states, which dominate
    (a deceased member is not "in arrears"); the rest are derived from the policy
    and the facts.
    """
    # lifecycle states, mirrored here so `standing` is always the whole answer
    SUSPENDED = "SUSPENDED", "Suspended"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"
    DECEASED = "DECEASED", "Deceased"
    CLOSED = "CLOSED", "Closed"
    PENDING = "PENDING", "Pending registration"
    # derived states
    GOOD = "GOOD", "Good standing"
    EXEMPT = "EXEMPT", "Exempt"
    GRACE = "GRACE", "Grace period"
    ARREARS = "ARREARS", "In arrears"
    INACTIVE = "INACTIVE", "Inactive"

    # NOTE: there is deliberately no `Standing.covered()` list here.
    #
    # There was, and it was wrong: a fixed list said that ARREARS meant "not
    # covered", when under a DEDUCT policy — the commonest real rule — an arrears
    # member is paid, with the arrears netted off. Whether a standing covers depends
    # on what the POLICY does about it, so the question is answered by
    # `StandingResult.covered`, which mirrors the eligibility engine's blocking
    # rules and shares its facts. A list here could only ever disagree with the
    # engine, and a register that contradicts the claim decision is worse than no
    # register.


# ---------------------------------------------------------------------------
# Exemptions
# ---------------------------------------------------------------------------

class MembershipExemption(models.Model):
    """A member excused from contributing, for a reason, for a period.

    Every scheme has them: the founding members, the very old, a family in
    genuine hardship. Without a first-class record, they are handled by a
    treasurer quietly not chasing certain people — which is indistinguishable
    from favouritism, cannot be handed over, and disappears when they do.

    An exemption is a POLICY DECISION about a member, so it is approved, dated,
    reasoned and audited like any other.
    """

    class Kind(models.TextChoices):
        LIFE = "LIFE", "Life member"
        AGE = "AGE", "Age"
        HARDSHIP = "HARDSHIP", "Hardship"
        BEREAVEMENT = "BEREAVEMENT", "Recently bereaved"
        SERVICE = "SERVICE", "Church service (pastor, elder)"
        OTHER = "OTHER", "Other"

    membership = models.ForeignKey("SchemeMembership", on_delete=models.CASCADE,
                                   related_name="exemptions")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    from_date = models.DateField(default=_dt.date.today)
    to_date = models.DateField(
        null=True, blank=True,
        help_text="Leave blank for an exemption with no end (a life member).")
    reason = models.TextField(
        help_text="Why. Kept on the permanent record — an exemption without a "
                  "recorded reason is indistinguishable from favouritism.")
    exempt_dues = models.BooleanField(
        default=True, help_text="Excused from periodic dues.")
    exempt_levies = models.BooleanField(
        default=False,
        help_text="Excused from per-case levies too. Rarer: most schemes still ask "
                  "an exempt member to stand with a bereaved family.")
    granted_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="An exemption relieves someone of a financial obligation, so it is "
                  "approved by someone other than whoever proposed it.")
    approved_at = models.DateTimeField(null=True, blank=True)
    revoked_on = models.DateField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-from_date"]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.membership}"

    @property
    def is_approved(self):
        return self.approved_by_id is not None

    def covers(self, on=None):
        """Live on a date. An UNAPPROVED exemption covers nothing: proposing that
        a member be excused does not excuse them."""
        on = on or _dt.date.today()
        if not self.is_approved or self.revoked_on and self.revoked_on <= on:
            return False
        if on < self.from_date:
            return False
        return self.to_date is None or on <= self.to_date

    def clean(self):
        if self.to_date and self.to_date < self.from_date:
            raise ValidationError("An exemption cannot end before it begins.")
        if not (self.reason or "").strip():
            raise ValidationError("An exemption must record why it was granted.")


# ---------------------------------------------------------------------------
# The membership event log
# ---------------------------------------------------------------------------

class MembershipEvent(models.Model):
    """Everything that has ever happened to a membership, in one narrative.

    `django-simple-history` already records every field change, and it is what an
    auditor uses to prove a value. But nobody can READ it: it answers "what was
    this field on 3 March?" and not "what happened to this member, and why?".

    This is the second thing, and it is the one a treasurer, a board and a
    bereaved family actually ask for. Every registration, admission, fee, renewal,
    suspension, exemption, transfer, reinstatement and death is one line here,
    with who did it and why.
    """

    class Kind(models.TextChoices):
        ENROLLED = "ENROLLED", "Enrolled"
        ADMITTED = "ADMITTED", "Admitted"
        REJECTED = "REJECTED", "Registration refused"
        FEE_PAID = "FEE_PAID", "Fee paid"
        RENEWED = "RENEWED", "Renewed"
        SUSPENDED = "SUSPENDED", "Suspended"
        REINSTATED = "REINSTATED", "Reinstated"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"
        DECEASED = "DECEASED", "Recorded as deceased"
        CLOSED = "CLOSED", "Closed"
        TRANSFERRED_OUT = "TRANS_OUT", "Membership transferred away"
        TRANSFERRED_IN = "TRANS_IN", "Membership taken over"
        EXEMPTED = "EXEMPTED", "Exemption granted"
        EXEMPT_ENDED = "EXEMPT_END", "Exemption ended"
        DEPENDANT_ADDED = "DEP_ADD", "Dependant registered"
        DEPENDANT_REMOVED = "DEP_REM", "Dependant removed"
        STANDING = "STANDING", "Standing changed"
        NOTE = "NOTE", "Note"

    membership = models.ForeignKey("SchemeMembership", on_delete=models.CASCADE,
                                   related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    on = models.DateField(default=_dt.date.today, db_index=True,
                          help_text="The date the thing happened (not when it was typed in).")
    summary = models.CharField(max_length=255)
    reason = models.TextField(blank=True)
    from_value = models.CharField(max_length=24, blank=True)
    to_value = models.CharField(max_length=24, blank=True)
    automated = models.BooleanField(
        default=False, db_index=True,
        help_text="Recorded by a job rather than a person. Kept visibly distinct: a "
                  "member has a right to know whether a human decided this.")
    actor = models.ForeignKey("auth.User", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-on", "-created_at"]
        indexes = [models.Index(fields=["membership", "-on"])]

    def __str__(self):
        return f"{self.on} {self.get_kind_display()} — {self.summary}"


# ---------------------------------------------------------------------------
# Household registration
# ---------------------------------------------------------------------------
#
# A household is NOT a second person-database. It is a REGISTRATION TYPE on the
# membership: one subscription, one principal member, a spouse and dependants who
# are all `members.Member` rows wherever the church knows them. The alternative —
# a Household model with its own names and phone numbers — would duplicate the
# member registry and guarantee the two drift apart, which is precisely what the
# brief says not to do.

class RegistrationType(models.TextChoices):
    INDIVIDUAL = "INDIVIDUAL", "Individual"
    HOUSEHOLD = "HOUSEHOLD", "Household"
