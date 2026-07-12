"""Phase 6 — committee roster management.

Two distinct questions, kept distinct on purpose:

    "Is this person ALLOWED to sit on a benevolent committee at all?"
        core.roles.can_vote_benevolent — the `benevolent_committee` right.
        A church-wide permission; unchanged by this module.

    "Is this person the SPECIFIC scheme's committee, and what is their seat?"
        This module. Per-scheme, role-aware, and — the important part —
        entirely OPTIONAL: a scheme with no roster configured here still lets
        anyone holding the right vote on its cases, exactly as before Phase 6.
        The roster only ever NARROWS who may vote, once a church sets one up.
"""
from __future__ import annotations

import datetime as _dt

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.utils import timezone

from benevolent.models import CommitteeMember


def roster(scheme, *, active_only=True):
    qs = CommitteeMember.objects.filter(scheme=scheme).select_related("user")
    if active_only:
        qs = qs.filter(active=True)
    return qs


def has_roster(scheme) -> bool:
    """Whether this scheme has actually configured a committee roster. Where it
    has not, `record_vote`/`committee_state` fall back to the plain right — see
    the module docstring."""
    return CommitteeMember.objects.filter(scheme=scheme, active=True).exists()


def is_seated(scheme, user) -> bool:
    """Does this person hold an active seat on THIS scheme's committee? Only
    meaningful once a roster exists at all — see has_roster."""
    if user is None:
        return False
    return CommitteeMember.objects.filter(scheme=scheme, user=user, active=True).exists()


def chair(scheme):
    return roster(scheme).filter(role=CommitteeMember.Role.CHAIR).first()


@db_tx.atomic
def add_member(scheme, user, *, role=CommitteeMember.Role.MEMBER, added_by=None):
    """Seat someone on a scheme's committee. Re-seating someone previously
    removed reactivates their original row rather than creating a duplicate —
    one seat, one history, however many times they come and go."""
    existing = CommitteeMember.objects.filter(scheme=scheme, user=user).first()
    if existing is not None:
        if existing.active:
            raise ValidationError(f"{user} already holds a seat on this committee.")
        existing.active = True
        existing.role = role
        existing.added_by = added_by
        existing.added_at = timezone.now()
        existing.removed_by = None
        existing.removed_at = None
        existing.removed_reason = ""
        existing.full_clean()
        existing.save()
        return existing

    member = CommitteeMember(scheme=scheme, user=user, role=role, added_by=added_by)
    member.full_clean()
    member.save()
    return member


@db_tx.atomic
def remove_member(seat, *, removed_by=None, reason=""):
    if not seat.active:
        raise ValidationError(f"{seat.user} is not currently seated.")
    seat.active = False
    seat.removed_by = removed_by
    seat.removed_at = timezone.now()
    seat.removed_reason = reason
    seat.save(update_fields=["active", "removed_by", "removed_at", "removed_reason"])
    return seat


@db_tx.atomic
def change_role(seat, *, role, changed_by=None):
    seat.role = role
    seat.full_clean()      # re-runs the one-Chair-per-scheme guard
    seat.save(update_fields=["role"])
    return seat
