"""The public benevolent application — a self-submitted, UNVERIFIED request.

This is NOT a membership. It is somebody's own account of who they are and who
depends on them, typed into a public form by a person nobody has yet checked.
It is deliberately a separate model from `SchemeMembership` for exactly that
reason: a registration officer reads it, verifies it, and only then registers
them — at which point the real membership is created through the same
`registry.register()` every other enrolment goes through.

Nothing here is covered. Nothing here owes dues. Nothing here can claim. A
public form that could create cover would be a public form that could create
liabilities, and no church should have one.

Security model follows the public pledge form (see `pledges/views.py`), which
was designed for exactly this problem:

  * Off unless explicitly enabled in settings.
  * Write-only. It never reads or exposes any member data — no autocomplete, no
    lookup, no roll. The applicant types their own details as free text.
  * A submission touches no ledger, no fund, no balance.
  * Honeypot, minimum fill time, and a per-session throttle against bots.
"""
import datetime as _dt

from django.db import models

from simple_history.models import HistoricalRecords


class BenevolentApplication(models.Model):
    """One person's public application to join a scheme."""

    class Standing(models.TextChoices):
        """What the applicant says they are. Their own claim, unverified — which
        is the whole point of this model. A registration officer checks it."""
        MEMBER = "MEMBER", "A registered church member"
        SABBATH_SCHOOL = "SABBATH_SCHOOL", "A Sabbath School member"
        VISITOR = "VISITOR", "A visitor / not yet a member"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting review"
        APPROVED = "APPROVED", "Approved and registered"
        REJECTED = "REJECTED", "Not accepted"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    scheme = models.ForeignKey("BenevolentScheme", on_delete=models.PROTECT,
                               related_name="applications")

    # --- what they say about themselves ------------------------------------
    full_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=32)
    email = models.EmailField(blank=True)
    standing = models.CharField(max_length=16, choices=Standing.choices,
                                default=Standing.VISITOR)
    date_of_birth = models.DateField(null=True, blank=True)
    national_id = models.CharField(
        max_length=32, blank=True,
        help_text="Optional. Some churches ask for it; many do not, and it is not "
                  "required to apply.")
    notes = models.TextField(blank=True, help_text="Anything else the applicant wanted "
                                                   "the church to know.")

    # --- review -------------------------------------------------------------
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    submitted_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reviewed_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=255, blank=True)

    # Set only on approval — the membership this application actually became, so
    # the paper trail runs from "somebody typed this into a form" all the way to
    # "and this is the cover it produced".
    membership = models.OneToOneField(
        "SchemeMembership", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="from_application")

    # A best-effort link to an existing church member, found by the reviewer (or
    # suggested by phone match). Never set by the applicant — they cannot search
    # the roll, by design.
    matched_member = models.ForeignKey(
        "members.Member", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="benevolent_applications")

    submitted_ip = models.GenericIPAddressField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [models.Index(fields=["status", "-submitted_at"])]

    def __str__(self):
        return f"{self.full_name} → {self.scheme.code} [{self.status}]"

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING

    @property
    def household_size(self):
        return 1 + self.dependants.count()


class ApplicationDependant(models.Model):
    """Someone the applicant says depends on them.

    Grouped into the three sections a church actually asks about — spouse,
    children, parents — because that is how a family is described out loud, and
    a single undifferentiated "dependants" list makes an applicant guess where
    their mother goes.

    Still just a claim. Nothing here is covered until a registration officer
    approves the application, at which point these become real
    `SchemeDependant` rows through the same service every other dependant goes
    through.
    """

    class Relationship(models.TextChoices):
        SPOUSE = "SPOUSE", "Spouse"
        CHILD = "CHILD", "Child"
        PARENT = "PARENT", "Parent"
        OTHER = "OTHER", "Other dependant"

    application = models.ForeignKey(BenevolentApplication, on_delete=models.CASCADE,
                                    related_name="dependants")
    relationship = models.CharField(max_length=8, choices=Relationship.choices)
    full_name = models.CharField(max_length=120)
    phone = models.CharField(
        max_length=32, blank=True,
        help_text="Optional, and worth asking for: a spouse or grown child very often "
                  "pays from their OWN line, and a number recorded here lets that "
                  "payment be matched to the family automatically instead of landing "
                  "in an unmatched queue.")
    date_of_birth = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["relationship", "id"]

    def __str__(self):
        return f"{self.full_name} ({self.get_relationship_display()})"
