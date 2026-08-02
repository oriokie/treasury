"""Approving pledges — one rule, two doors.

A treasurer approves anything. A department leader approves pledges made to a
campaign for a fund they lead, and nothing else. Both go through here, so the
scope check is written once and a bulk action cannot take a shortcut a single
action would have refused.

Approval is what turns a promise into a figure the campaign counts, which is
why a leader gets it: they run the appeal, they know who actually stood up, and
routing every one of those through the treasurer is how a campaign's numbers
end up a fortnight behind the room.
"""
from django.utils import timezone

from pledges.models import Pledge


def approvable_for(user):
    """The draft pledges this user may approve.

    A treasurer sees every draft. A leader sees drafts on campaigns targeting a
    fund they lead — including a sub-account of it, since a campaign is often
    run against a child fund of the department the leader is given.
    """
    from core import roles
    qs = (Pledge.objects.filter(status=Pledge.Status.DRAFT)
          .select_related("member", "campaign", "campaign__target_department",
                          "recorded_by")
          .order_by("-created_at", "-id"))
    if roles.can_approve(user):
        return qs
    from leaders.permissions import allowed_departments
    dept_ids = set(allowed_departments(user).values_list("id", flat=True))
    if not dept_ids:
        return qs.none()
    from departments.models import Department
    # a campaign on a sub-account of a fund the leader holds is still theirs
    child_ids = set(Department.objects.filter(parent_id__in=dept_ids)
                    .values_list("id", flat=True))
    return qs.filter(campaign__target_department_id__in=dept_ids | child_ids)


def may_approve(user, pledge):
    return approvable_for(user).filter(pk=pledge.pk).exists()


def approve(pledge, user):
    """Approve one draft. Returns True if it changed."""
    if pledge.status != Pledge.Status.DRAFT:
        return False
    pledge.status = Pledge.Status.ACTIVE
    pledge.approved_by = user
    pledge.approved_at = timezone.now()
    pledge.save(update_fields=["status", "approved_by", "approved_at"])
    # a pledge approved with money already matched against it is fulfilled the
    # moment it becomes active; recompute rather than leaving it merely ACTIVE
    pledge.recompute_status()
    return True


def approve_many(pledge_ids, user):
    """Approve a batch, silently skipping anything outside the user's scope.

    Scope is re-derived here rather than trusted from the form: a list of ids in
    a POST is a claim, not a permission. Returns (approved, skipped).
    """
    ids = {int(i) for i in pledge_ids if str(i).isdigit()}
    if not ids:
        return 0, 0
    allowed = list(approvable_for(user).filter(pk__in=ids))
    approved = sum(1 for p in allowed if approve(p, user))
    return approved, len(ids) - approved
