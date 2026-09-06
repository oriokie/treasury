"""Detect pledge / member / campaign / group codes inside bank references.

Codes are matched FIRST — before payer identity — so one person can pay
toward another's pledge, development-group tally, or campaign slot.

Matching is **contains** on a punctuation-stripped reference, so surrounding
text or separators (``FOR MB7K2M``, ``MB-7K2M``, ``gift/MB7K2M``) all work.

Priority (longest match within each class; classes in this order):
  1. Pledge-specific ``PG…`` code → that pledge + its member
  2. Member ``MB…`` code → that register member (pledges + their group)
  3. Campaign-member ``CM…`` code → campaign sheet person + group
  4. Development-group ``DEV…`` code → Development fund + that group
"""
import re

from giving.services.allocation import normalize_reference


def _norm_code(code):
    return re.sub(r"[^a-z0-9]", "", (code or "").strip().lower())


def codes_in_reference(reference):
    """Normalised reference for substring code detection.

    Strips whitespace *and* punctuation so ``MB-ABCD`` / ``MB ABCD`` still
    contain the stored code ``MBABCD``.
    """
    # Start from the usual whitespace fold, then drop remaining non-alnum so
    # hyphens / slashes / stars typed around a code cannot hide it.
    return _norm_code(normalize_reference(reference))


def _first_code_hit(s, items, attr="match_code"):
    """Longest normalised code that appears as a substring of ``s``."""
    ranked = sorted(
        ((_norm_code(getattr(obj, attr, "") or ""), obj) for obj in items),
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


def find_dev_group_by_code(reference):
    """Return the DevelopmentGroup whose ``DEV…`` match_code is in the ref."""
    from departments.models import DevelopmentGroup
    s = codes_in_reference(reference)
    if not s:
        return None
    groups = list(
        DevelopmentGroup.objects.filter(active=True, match_code__gt="")
    )
    return _first_code_hit(s, groups)


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


def resolve_dev_group_for_beneficiary(ben, campaign_group=""):
    """Pick an existing DevelopmentGroup for a credited person — never create.

    Order: member's home group → campaign sheet group string (contains match)
    → active development campaign membership for that person.
    """
    from departments.services.matching import match_existing_dev_group
    if ben is not None and getattr(ben, "dev_group_id", None):
        return ben.dev_group
    if campaign_group:
        hit = match_existing_dev_group(campaign_group)
        if hit is not None:
            return hit
    if ben is None:
        return None
    # Look for an active campaign sheet row for this person whose group
    # string maps to an existing DevelopmentGroup.
    from giving.models import CampaignMember
    from members.models import normalize_phone
    ph = normalize_phone(getattr(ben, "phone", "") or "")
    qs = CampaignMember.objects.filter(campaign__active=True).exclude(group="")
    rows = []
    if ph:
        rows = list(qs.filter(phone=ph)[:5])
    if not rows and getattr(ben, "name_key", None):
        rows = list(qs.filter(name_key=ben.name_key)[:5])
    for cm in rows:
        hit = match_existing_dev_group(cm.group)
        if hit is not None:
            return hit
    return None


def _development_fund():
    from departments.models import Department
    dept = (Department.objects
            .filter(category=Department.Category.DEVELOPMENT,
                    parent__isnull=True, active=True)
            .order_by("id").first())
    if dept:
        return dept
    return Department.objects.filter(name__iexact="development").first()


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
      dev_group     — DevelopmentGroup from the beneficiary / DEV code
      campaign, campaign_group — when a CM code hit
      via_code      — True (caller stamps Transaction.attributed_via_code)
      source        — "pledge" | "member" | "campaign" | "dev_group"

    Payer identity is left on payer_name / payer_phone; ``beneficiary`` is who
    the gift is *for*. Callers set Transaction.member = beneficiary so
    development-group member reports credit the right person.
    """
    # 1. Specific pledge code
    pledge = find_pledge_by_code(reference)
    if pledge is not None:
        ben = pledge.member
        dg = resolve_dev_group_for_beneficiary(ben)
        dept = getattr(pledge.campaign, "target_department", None)
        if dept is None and dg is not None:
            dept = _development_fund()
        return {
            "beneficiary": ben,
            "pledge": pledge,
            "department": dept,
            "dev_group": dg,
            "campaign": None,
            "campaign_group": "",
            "via_code": True,
            "source": "pledge",
        }

    # 2. Register member code (one code for pledges + group contributions)
    member = find_member_by_code(reference)
    if member is not None:
        dept = None
        from pledges.models import Pledge
        open_p = (Pledge.objects.filter(
            member=member, status__in=Pledge.RECOGNISED_STATUSES,
            campaign__target_department__isnull=False)
            .select_related("campaign", "campaign__target_department")
            .order_by("-start_date", "-id").first())
        if open_p is not None:
            dept = open_p.campaign.target_department
        dg = resolve_dev_group_for_beneficiary(member)
        # No open pledge fund but we know their group → Development AUTO
        if dept is None and dg is not None:
            dept = _development_fund()
        return {
            "beneficiary": member,
            "pledge": open_p,
            "department": dept,
            "dev_group": dg,
            "campaign": None,
            "campaign_group": "",
            "via_code": True,
            "source": "member",
        }

    # 3. Campaign sheet code
    camp, cm = find_campaign_member_by_code(reference)
    if camp is not None and cm is not None:
        from departments.models import Department
        ben = resolve_campaign_member_to_register(cm)
        dg = resolve_dev_group_for_beneficiary(ben, cm.group or "")
        # Development campaigns: keep the parent fund and tag the group —
        # never spawn a child Department named after the group number.
        if (camp.department
                and camp.department.category == Department.Category.DEVELOPMENT):
            dept = camp.department
        else:
            dept = camp.subgroup_department(cm.group)
        if dept is None and dg is not None:
            dept = _development_fund()
        return {
            "beneficiary": ben,
            "pledge": None,
            "department": dept,
            "dev_group": dg,
            "campaign": camp,
            "campaign_group": cm.group or "",
            "via_code": True,
            "source": "campaign",
        }

    # 4. Development-group code (group-only; no person redirect)
    grp = find_dev_group_by_code(reference)
    if grp is not None:
        return {
            "beneficiary": payer_member,
            "pledge": None,
            "department": _development_fund(),
            "dev_group": grp,
            "campaign": None,
            "campaign_group": "",
            "via_code": False,
            "source": "dev_group",
        }

    return {}
