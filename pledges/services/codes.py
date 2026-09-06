"""Detect pledge / member / campaign codes inside bank / M-Pesa references.

Codes are matched FIRST — before payer identity — so one person can pay
toward another's pledge, development-group tally, or campaign slot.

Priority (longest match within each class; classes in this order):
  1. Pledge-specific ``PG…`` code → that pledge + its member
  2. Member ``MB…`` code → that register member (pledges + their group)
  3. Campaign-member ``CM…`` code → campaign sheet person + group fund
"""
import re

from giving.services.allocation import normalize_reference


def _norm_code(code):
    return re.sub(r"[^a-z0-9]", "", (code or "").strip().lower())


def codes_in_reference(reference):
    """Normalised reference text used for substring code detection."""
    return normalize_reference(reference)


def _first_code_hit(s, items, attr="match_code"):
    """Longest normalised code that appears as a substring of ``s``."""
    ranked = sorted(
        (( _norm_code(getattr(obj, attr, "") or ""), obj) for obj in items),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for code, obj in ranked:
        if len(code) >= 4 and code in s:
            return obj
    return None


def find_pledge_by_code(reference):
    """Return the recognised pledge whose match_code appears in ``reference``."""
    from pledges.models import Pledge
    s = codes_in_reference(reference)
    if not s:
        return None
    pledges = list(
        Pledge.objects.filter(
            status__in=Pledge.RECOGNISED_STATUSES,
            match_code__gt="",
        ).select_related("campaign", "campaign__target_department", "member",
                         "member__dev_group")
    )
    return _first_code_hit(s, pledges)


def find_member_by_code(reference):
    """Return the active Member whose ``match_code`` appears in ``reference``."""
    from members.models import Member
    s = codes_in_reference(reference)
    if not s:
        return None
    members = list(
        Member.objects.filter(active=True, match_code__gt="")
        .select_related("dev_group")
    )
    return _first_code_hit(s, members)


def find_campaign_member_by_code(reference):
    """Return (Campaign, CampaignMember) when a CM match_code is in the ref."""
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
    hit = _first_code_hit(s, members)
    if hit is None:
        return None, None
    return hit.campaign, hit


def resolve_campaign_member_to_register(cm):
    """Best-effort link from a campaign sheet row to the church Member register.

    Phone (primary or alternate) first, then a unique name_key. Returns None
    when nothing unambiguous is found — the fund still routes via the campaign
    group, but there is no register member to credit on tallies / "paid by".
    """
    from django.db.models import Q
    from members.models import Member, normalize_phone
    ph = normalize_phone(cm.phone)
    if ph:
        ben = (Member.objects.filter(active=True)
               .filter(Q(phone=ph) | Q(phones__number=ph))
               .distinct().first())
        if ben is not None:
            return ben
    if cm.name_key:
        qs = Member.objects.filter(name_key=cm.name_key, active=True)
        if qs.count() == 1:
            return qs.first()
    return None


def pledge_code_allocate(reference):
    """If a pledge match_code is in the reference, return fund routing for it.

    Returns (pledge, department, status) or (None, None, None).
    """
    pledge = find_pledge_by_code(reference)
    if pledge is None:
        return None, None, None
    dept = pledge.campaign.target_department
    if dept is None:
        return pledge, None, None
    return pledge, dept, "AUTO"


def resolve_code_attribution(reference, payer_member=None):
    """Interpret codes in ``reference`` for import / matching.

    Returns a dict (empty when no code hits):
      beneficiary   — Member who should receive credit (tallies / pledges)
      pledge        — specific Pledge when a PG code hit
      department    — fund to route to when known
      dev_group     — DevelopmentGroup from the beneficiary when set
      campaign, campaign_group — when a CM code hit
      via_code      — True (caller stamps Transaction.attributed_via_code)
      source        — "pledge" | "member" | "campaign"

    Payer identity is left on payer_name / payer_phone; ``beneficiary`` is who
    the gift is *for*. Callers set Transaction.member = beneficiary so
    development-group member reports credit the right person.

    Pledge (PG), member (MB) and campaign (CM) codes all share this path — so
    "paid by others" / group tallies behave the same whichever code was used.
    """
    # 1. Specific pledge code
    pledge = find_pledge_by_code(reference)
    if pledge is not None:
        ben = pledge.member
        return {
            "beneficiary": ben,
            "pledge": pledge,
            "department": getattr(pledge.campaign, "target_department", None),
            "dev_group": getattr(ben, "dev_group", None) if ben else None,
            "campaign": None,
            "campaign_group": "",
            "via_code": True,
            "source": "pledge",
        }

    # 2. Register member code (one code for pledges + group contributions)
    member = find_member_by_code(reference)
    if member is not None:
        dept = None
        # Prefer an open pledge's campaign fund when the gift is otherwise
        # unallocated — the code says this gift is for that member's appeal.
        from pledges.models import Pledge
        open_p = (Pledge.objects.filter(
            member=member, status__in=Pledge.RECOGNISED_STATUSES,
            campaign__target_department__isnull=False)
            .select_related("campaign", "campaign__target_department")
            .order_by("-start_date", "-id").first())
        if open_p is not None:
            dept = open_p.campaign.target_department
        return {
            "beneficiary": member,
            "pledge": open_p,
            "department": dept,
            "dev_group": member.dev_group,
            "campaign": None,
            "campaign_group": "",
            "via_code": True,
            "source": "member",
        }

    # 3. Campaign sheet code — same attribution as member codes once linked
    # to a register Member (phone / unique name).
    camp, cm = find_campaign_member_by_code(reference)
    if camp is not None and cm is not None:
        ben = resolve_campaign_member_to_register(cm)
        return {
            "beneficiary": ben,
            "pledge": None,
            "department": camp.subgroup_department(cm.group),
            "dev_group": getattr(ben, "dev_group", None) if ben else None,
            "campaign": camp,
            "campaign_group": cm.group or "",
            "via_code": True,
            "source": "campaign",
        }

    return {}
