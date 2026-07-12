"""Phase 6 — committee membership, roles, and approval levels.

Sitting on the benevolent committee is already its own permission
(`core.roles.can_vote_benevolent`, the `benevolent_committee` right) —
deliberately not folded into the treasurer role, because the whole point of a
committee is that it is a body distinct from the person who pays. That answers
WHO MAY EVER vote on ANY scheme's committee.

This module answers a narrower, additional question a church with more than
one scheme actually has: WHICH scheme's committee is this person actually on,
and what is their SEAT — an ordinary member, or the Chair, whose presence a
policy can require before a decision carries. A right is binary; a roster has
roles and can be configured per scheme.

Deliberately additive, not a replacement: a scheme with no roster configured
here still lets anyone holding the general right vote, exactly as before —
the roster only NARROWS who may vote once a church actually sets one up, and
never requires a church that is happy with the simple global right to
configure anything at all.
"""
import datetime as _dt

from django.core.exceptions import ValidationError
from django.db import models


class CommitteeMember(models.Model):
    """One seat on one scheme's committee."""

    class Role(models.TextChoices):
        CHAIR = "CHAIR", "Chair"
        VICE_CHAIR = "VICE_CHAIR", "Vice-chair"
        SECRETARY = "SECRETARY", "Secretary"
        TREASURER = "TREASURER", "Committee treasurer"
        MEMBER = "MEMBER", "Member"

    scheme = models.ForeignKey("BenevolentScheme", on_delete=models.CASCADE,
                               related_name="committee_members")
    user = models.ForeignKey("auth.User", on_delete=models.PROTECT,
                             related_name="benevolent_committee_seats")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    active = models.BooleanField(default=True, db_index=True)

    added_by = models.ForeignKey("auth.User", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)
    removed_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["scheme", "role", "user__username"]
        constraints = [models.UniqueConstraint(fields=["scheme", "user"],
                                               name="uniq_committee_seat_per_scheme")]

    def __str__(self):
        return f"{self.user} — {self.get_role_display()} ({self.scheme.code})"

    def clean(self):
        if self.role == self.Role.CHAIR and self.active:
            existing = CommitteeMember.objects.filter(
                scheme_id=self.scheme_id, role=self.Role.CHAIR, active=True
            ).exclude(pk=self.pk)
            if existing.exists():
                raise ValidationError(
                    "This scheme already has an active Chair. Remove them from the "
                    "chair first — a committee has one at a time, or a quorum "
                    "requirement built around 'the Chair's vote' stops meaning "
                    "anything.")
