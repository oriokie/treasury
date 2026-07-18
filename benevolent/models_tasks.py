"""Review tasks — where automation asks a human to decide.

The benevolent module's automation writes only to derived state (the standing
cache), never to `status`, because a status change — suspending a member, closing
a membership, dropping a dependant's cover — is a decision a person should make
and answer for. But automation is exactly what NOTICES that such a decision is due:
it is the nightly job that sees a member has missed six levies, or that a child
dependant has just passed the policy's age limit.

A `BenevolentTask` is how those two facts are reconciled. The job does not act; it
raises a task that STATES what it found, what the policy would do about it, and
leaves a human to confirm or dismiss. The member's status is untouched until
somebody clicks. This keeps the "a person decides" rule intact while still making
sure the thing that needs deciding does not sit unseen in a register of four
hundred members.

Tasks are deliberately IDEMPOTENT to raise: a task carries a `dedup_key` unique
among open tasks, so a nightly job that runs every night does not raise the same
"suspend Jane" task thirty times. Re-running the job is free; it either finds the
open task already there and leaves it, or raises it once.
"""
from __future__ import annotations

from django.db import models
from simple_history.models import HistoricalRecords


class BenevolentTask(models.Model):
    """A thing a human needs to look at, raised by automation (or by hand).

    Never changes anything itself. Resolving it is what a person does next —
    usually by following the linked object and taking the action the task
    describes — after which the task is marked done. The task is a prompt, not an
    instruction the system will carry out on its own.
    """

    class Kind(models.TextChoices):
        SUSPEND_OVERDUE = "SUSPEND_OVERDUE", "Consider suspending an overdue member"
        CLOSE_INACTIVE = "CLOSE_INACTIVE", "Consider closing an inactive membership"
        DEPENDANT_AGED_OUT = "DEPENDANT_AGED_OUT", "Dependant has passed the age limit"
        POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE", "Possible duplicate membership"
        WAITING_PERIOD_SERVED = "WAITING_PERIOD_SERVED", "Member has become eligible"
        RENEWAL_DUE = "RENEWAL_DUE", "Membership renewal is due"
        OTHER = "OTHER", "Other"

    class Severity(models.TextChoices):
        HIGH = "HIGH", "High"
        MEDIUM = "MEDIUM", "Medium"
        LOW = "LOW", "Low"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        DONE = "DONE", "Actioned"
        DISMISSED = "DISMISSED", "Dismissed"

    scheme = models.ForeignKey("BenevolentScheme", on_delete=models.CASCADE,
                               related_name="tasks")
    kind = models.CharField(max_length=24, choices=Kind.choices, db_index=True)
    severity = models.CharField(max_length=6, choices=Severity.choices,
                                default=Severity.MEDIUM)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.OPEN, db_index=True)

    title = models.CharField(max_length=160)
    detail = models.TextField(blank=True)

    # what the task is about — any of these may be set
    membership = models.ForeignKey("SchemeMembership", null=True, blank=True,
                                   on_delete=models.CASCADE, related_name="tasks")
    dependant = models.ForeignKey("SchemeDependant", null=True, blank=True,
                                  on_delete=models.CASCADE, related_name="tasks")
    case = models.ForeignKey("BenevolentCase", null=True, blank=True,
                             on_delete=models.CASCADE, related_name="tasks")

    # the action the policy WOULD take, recorded so the human can see the
    # recommendation without re-deriving it. Never applied automatically.
    recommended_action = models.CharField(max_length=40, blank=True)

    # idempotency: unique among OPEN tasks, so a nightly job re-raising the same
    # finding does not pile up duplicates. Enforced in the service layer because
    # MariaDB does not reliably enforce a conditional unique constraint.
    dedup_key = models.CharField(max_length=120, db_index=True)

    created_by_automation = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=200, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "kind"]),
            models.Index(fields=["scheme", "status"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.title}"

    @property
    def is_open(self):
        return self.status == self.Status.OPEN
