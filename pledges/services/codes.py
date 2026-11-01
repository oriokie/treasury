"""Detect pledge / member / campaign / group codes inside bank references.

Codes are matched FIRST — before payer identity — so one person can pay
toward another's pledge, development-group tally, or campaign slot.

Matching is **contains** on a punctuation-stripped reference, so surrounding
text or separators (``FOR MB7K2M``, ``MB-7K2M``, ``gift/MB7K2M``) all work.

Priority (longest match within each class; classes in this order):
  1. Pledge-specific ``PG…`` code → that pledge + its member
  2. Pledge-campaign ``PC…`` code → that appeal's target fund (general gift)
  3. Member ``MB…`` code → that register member + Development fund + group
     (group: active campaign sheet → home → optional history; never a
     closed/inactive campaign's fund — use PG… / CM… / PC… for campaign funds)
  4. Campaign-member ``CM…`` code → campaign sheet person + group
  5. Development-group ``DEV…`` code → Development fund + that group
"""
import datetime as dt
import re

from django.db.models import Count, Sum

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


def find_pledge_campaign_by_code(reference):
    """Return the ACTIVE PledgeCampaign whose ``PC…`` match_code is in the ref."""
    from pledges.models import PledgeCampaign
    s = codes_in_reference(reference)
    if not s:
        return None
    camps = list(
        PledgeCampaign.objects.filter(
            status=PledgeCampaign.Status.ACTIVE,
            match_code__gt="",
        ).select_related("target_department")
    )
    return _first_code_hit(s, camps)


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

    Used by PG… / CM… / DEV paths. MB… uses ``resolve_mb_dev_group`` instead
    (sheet before home, plus optional history).
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
    sheet = _active_campaign_member_rows(ben)
    for cm in sheet:
        hit = match_existing_dev_group(cm.group)
        if hit is not None:
            return hit
    return None


def _active_campaign_member_rows(member):
    """Active CampaignMember sheet rows for this register member (phone, then name)."""
    from giving.models import CampaignMember
    from members.models import normalize_phone
    if member is None:
        return []
    qs = (CampaignMember.objects.filter(campaign__active=True)
          .exclude(group="")
          .select_related("campaign", "campaign__department")
          .order_by("-id"))
    ph = normalize_phone(getattr(member, "phone", "") or "")
    rows = []
    if ph:
        rows = list(qs.filter(phone=ph)[:10])
    if not rows and getattr(member, "name_key", None):
        rows = list(qs.filter(name_key=member.name_key)[:10])
    return rows


def _pick_sheet_group(rows):
    """Best sheet row whose group maps to an existing DevelopmentGroup.

    Prefer a DEVELOPMENT campaign when several map; then newest id.
    Returns (DevelopmentGroup, CampaignMember) or (None, None).
    """
    from departments.models import Department
    from departments.services.matching import match_existing_dev_group
    mapped = []
    for cm in rows:
        hit = match_existing_dev_group(cm.group)
        if hit is None:
            continue
        is_dev = bool(
            cm.campaign.department_id
            and cm.campaign.department.category == Department.Category.DEVELOPMENT)
        mapped.append((1 if is_dev else 0, cm.id, hit, cm))
    if not mapped:
        return None, None
    mapped.sort(key=lambda t: (t[0], t[1]), reverse=True)
    _dev, _id, dg, cm = mapped[0]
    return dg, cm


def _history_dev_group(member, months):
    """Group this member has given the most tagged KES to in the last ``months``.

    Only gifts already stamped with a ``dev_group`` count — never invents a
    group from untagged money. Ties: higher gift count, then lower group id.
    """
    from giving.models import Transaction
    months = max(1, min(24, int(months or 6)))
    since = dt.date.today() - dt.timedelta(days=30 * months)
    rows = list(
        Transaction.objects.filter(
            member=member,
            direction=Transaction.Direction.CREDIT,
            confirmed=True,
            is_reversal=False,
            is_reversed=False,
            dev_group__isnull=False,
            date__gte=since,
        ).values("dev_group")
        .annotate(total=Sum("amount"), n=Count("id"))
        .order_by("-total", "-n", "dev_group")
    )
    if not rows:
        return None
    from departments.models import DevelopmentGroup
    return DevelopmentGroup.objects.filter(pk=rows[0]["dev_group"]).first()


def resolve_mb_dev_group(member, cfg=None):
    """Group resolution for an MB… code — never creates groups.

    Cascade:
      1. active campaign sheet (group maps to an existing DevelopmentGroup)
      2. member's home ``dev_group``
      3. optional history (most tagged KES in the last N months)
      4. none

    Returns a dict:
      dev_group, campaign, campaign_group, group_rule
      (group_rule: ``campaign_sheet`` | ``home`` | ``history`` | ``none``)
    """
    from core.models import SiteConfig
    cfg = cfg or SiteConfig.get()
    empty = {
        "dev_group": None, "campaign": None, "campaign_group": "",
        "group_rule": "none",
    }
    if member is None:
        return empty

    rows = _active_campaign_member_rows(member)
    dg, cm = _pick_sheet_group(rows)
    if dg is not None and cm is not None:
        return {
            "dev_group": dg,
            "campaign": cm.campaign,
            "campaign_group": cm.group or "",
            "group_rule": "campaign_sheet",
        }

    if getattr(member, "dev_group_id", None):
        return {
            "dev_group": member.dev_group,
            "campaign": None,
            "campaign_group": "",
            "group_rule": "home",
        }

    if getattr(cfg, "mb_code_group_history", False):
        months = getattr(cfg, "mb_code_group_history_months", 6) or 6
        hist = _history_dev_group(member, months)
        if hist is not None:
            return {
                "dev_group": hist,
                "campaign": None,
                "campaign_group": "",
                "group_rule": "history",
            }

    return empty


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

    # 2. Campaign-level PC… code — general gift to that appeal's fund.
    pcamp = find_pledge_campaign_by_code(reference)
    if pcamp is not None:
        dept = getattr(pcamp, "target_department", None)
        if dept is None:
            dept = _development_fund()
        return {
            "beneficiary": payer_member,
            "pledge": None,
            "department": dept,
            "dev_group": resolve_dev_group_for_beneficiary(payer_member),
            "campaign": None,
            "campaign_group": "",
            "via_code": False,
            "source": "pledge_campaign",
            "pledge_campaign": pcamp,
        }

    # 3. Register member code — always Development fund; group via cascade.
    # Closed / any pledge campaign is never used to pick the fund (use PG…).
    member = find_member_by_code(reference)
    if member is not None:
        grp = resolve_mb_dev_group(member)
        return {
            "beneficiary": member,
            "pledge": None,
            "department": _development_fund(),
            "dev_group": grp["dev_group"],
            "campaign": grp["campaign"],
            "campaign_group": grp["campaign_group"],
            "via_code": True,
            "source": "member",
            "group_rule": grp["group_rule"],
        }

    # 4. Campaign sheet code
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

    # 5. Development-group code (group-only; no person redirect)
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
