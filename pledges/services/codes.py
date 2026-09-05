"""Detect pledge / campaign member codes inside bank / M-Pesa references.

Codes are matched FIRST — before payer identity — so a pledged member can
contribute toward another member's pledge (or rally gifts for a campaign
member) by putting that person's code in the reference.
"""
import re

from giving.services.allocation import normalize_reference


def _norm_code(code):
    return re.sub(r"[^a-z0-9]", "", (code or "").strip().lower())


def codes_in_reference(reference):
    """Normalised reference text used for substring code detection."""
    return normalize_reference(reference)


def find_pledge_by_code(reference):
    """Return the recognised pledge whose match_code appears in ``reference``.

    Longest code wins when several would match (avoids a short code swallowing
    a longer one). Only ACTIVE / FULFILLED / LAPSED pledges — drafts and
    cancelled never receive money via a code.
    """
    from pledges.models import Pledge
    s = codes_in_reference(reference)
    if not s:
        return None
    # Prefetch candidates whose code could appear; scan longest-first.
    pledges = list(
        Pledge.objects.filter(
            status__in=Pledge.RECOGNISED_STATUSES,
            match_code__gt="",
        ).select_related("campaign", "campaign__target_department", "member")
    )
    pledges.sort(key=lambda p: len(_norm_code(p.match_code)), reverse=True)
    for p in pledges:
        code = _norm_code(p.match_code)
        if len(code) >= 4 and code in s:
            return p
    return None


def find_campaign_member_by_code(reference):
    """Return (Campaign, CampaignMember) when a member match_code is in the ref.

    Longest code wins. Only members of active campaigns.
    """
    from giving.models import CampaignMember
    s = codes_in_reference(reference)
    if not s:
        return None, None
    members = list(
        CampaignMember.objects.filter(
            match_code__gt="",
            campaign__active=True,
        ).select_related("campaign", "campaign__department")
    )
    members.sort(key=lambda m: len(_norm_code(m.match_code)), reverse=True)
    for m in members:
        code = _norm_code(m.match_code)
        if len(code) >= 4 and code in s:
            return m.campaign, m
    return None, None


def pledge_code_allocate(reference):
    """If a pledge match_code is in the reference, return fund routing for it.

    Returns (pledge, department, status) or (None, None, None).
    status is always AUTO when a code hits — the code is explicit intent.
    """
    pledge = find_pledge_by_code(reference)
    if pledge is None:
        return None, None, None
    dept = pledge.campaign.target_department
    if dept is None:
        return pledge, None, None
    return pledge, dept, "AUTO"
