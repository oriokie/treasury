"""Reference -> department allocation engine.

The bank narration reference is free text typed by the giver, so it is messy:
'DEVGR7', 'devg14', 'Devgrp11', 'DEVLOP GP14', 'dev grp5', 'DEv Gp39',
'DEVGRP3*', ' DEVGR26', 'dev', 'TITHE', 'ss', 'hosministry', ...

allocate() normalises it and resolves, in order:
  1. a development-group number (returns a 'DEV_GROUP_<n>' token, or
     'DEV_GROUP_NA' when it's clearly development but has no number),
  2. a seeded/learned AllocationRule,
  3. otherwise 'UNALLOCATED' -> the review queue.
"""
import re

import datetime as _dt
from giving.models import AllocationRule

_MIN = _dt.date.min
_MAX = _dt.date.max

# Matches the many dev-group spellings. Looks for a dev/grp marker then a number.
DEV_NUM_RE = re.compile(r"(?:dev(?:e?l?o?p?)?(?:gr(?:ou)?p?|gp|g)?|gr(?:ou)?p|gp)0*(\d+)")
# A reference that mentions development at all (even without a number).
DEV_WORD_RE = re.compile(r"(?:dev(?:elop)?|grp|group|gp)")


def normalize_reference(reference):
    return re.sub(r"\s+", "", (reference or "").strip().lower())


def _rule_sort_key(r):
    """Higher tuple = preferred when several rules match the same reference:
      1. a period-specific rule that covers the date beats a permanent one;
      2. the most recently-starting period wins;
      3. an explicit split fund beats a stray department rule (deliberate config
         beats a one-off 'remember this' that may have been a mistake);
      4. the newest rule (highest id) wins remaining ties — latest intent.
    """
    return (
        1 if r.is_period else 0,
        r.valid_from or _MIN,
        1 if r.split_fund_id else 0,
        r.pk or 0,
    )


def _pick(rules, date):
    """From candidate rules, choose the best one for `date` (see _rule_sort_key)."""
    covering = [r for r in rules if r.covers(date)]
    if not covering:
        return None
    covering.sort(key=_rule_sort_key, reverse=True)
    return covering[0]


def _result(rule):
    target = rule.split_fund or rule.department
    return target, ("AUTO" if rule.source == AllocationRule.Source.SEED else "LEARNED")


def allocate(reference, date=None):
    """Return (resolver, status).

    resolver: a Department, a 'DEV_GROUP_<n>'/'DEV_GROUP_NA' token, or 'UNALLOCATED'.
    status:   'AUTO' | 'LEARNED' | 'REVIEW'.
    Period-scoped rules that cover `date` take precedence over permanent rules.
    """
    raw = (reference or "").strip().lower()
    s = normalize_reference(reference)
    if not s:
        return "UNALLOCATED", "REVIEW"

    m = DEV_NUM_RE.search(s)
    if m and 1 <= int(m.group(1)) <= 99:
        return f"DEV_GROUP_{int(m.group(1))}", "AUTO"

    # church-configured numbered fund families, e.g. EXPENSE<n> -> fund "CAMP_<n>".
    # One config line covers every group; resolves only when that fund exists.
    for prefixes, template in _numbered_fund_families():
        fm = re.search(r"(?:%s)[ _-]*0*(\d+)" % "|".join(prefixes), s)
        if fm:
            from departments.models import Department
            name = template.replace("{n}", str(int(fm.group(1))))
            dept = Department.objects.filter(name__iexact=name).first()
            if dept:
                return dept, "AUTO"

    # church-configured extra prefixes (e.g. "project", "phase")
    extra = _extra_dev_prefixes()
    if extra:
        m2 = re.search(r"(?:%s)0*(\d+)" % "|".join(extra), s)
        if m2 and 1 <= int(m2.group(1)) <= 99:
            return f"DEV_GROUP_{int(m2.group(1))}", "AUTO"

    exact = list(AllocationRule.objects.filter(reference=s).select_related(
        "department", "split_fund"))
    rule = _pick(exact, date)
    if rule:
        return _result(rule)

    # pattern rules (starts-with / ends-with / contains): most specific first
    patterns = list(AllocationRule.objects.exclude(
        match_type=AllocationRule.MatchType.EXACT).select_related(
        "department", "split_fund"))
    order = {AllocationRule.MatchType.STARTS: 0, AllocationRule.MatchType.ENDS: 1,
             AllocationRule.MatchType.CONTAINS: 2}
    patterns.sort(key=lambda r: (order.get(r.match_type, 3), -len(r.reference),
                                  0 if r.split_fund_id else 1, -(r.pk or 0)))
    matched = []
    for r in patterns:
        ref = r.reference
        if not ref:
            continue
        if r.match_type == AllocationRule.MatchType.REGEX:
            try:
                hit = bool(re.search(ref, s))
            except re.error:
                hit = False        # a malformed pattern never matches (and never crashes)
        else:
            hit = ((r.match_type == AllocationRule.MatchType.STARTS and s.startswith(ref))
                   or (r.match_type == AllocationRule.MatchType.ENDS and s.endswith(ref))
                   or (r.match_type == AllocationRule.MatchType.CONTAINS and ref in s))
        if hit:
            matched.append(r)
    # prefer a period rule covering the date, keeping the most-specific match order
    period_hits = [r for r in matched if r.is_period and r.covers(date)]
    if period_hits:
        return _result(period_hits[0])
    perm_hits = [r for r in matched if not r.is_period]
    if perm_hits:
        return _result(perm_hits[0])

    # development without a usable number -> still clearly a dev-group gift
    if DEV_WORD_RE.search(s):
        return "DEV_GROUP_NA", "AUTO"

    return "UNALLOCATED", "REVIEW"


def _extra_dev_prefixes():
    """Normalised extra dev-group prefixes from SiteConfig, or []. Cheap + tolerant."""
    try:
        from core.models import SiteConfig
        raw = SiteConfig.get().dev_group_extra_prefixes or ""
    except Exception:
        return []
    out = []
    for part in raw.split(","):
        p = re.sub(r"[^a-z0-9]", "", part.strip().lower())
        if p:
            out.append(re.escape(p))
    return out


def _numbered_fund_families():
    """Parse SiteConfig.numbered_fund_families into [(prefixes, template), ...].

    Each non-empty line is 'prefix1, prefix2 = NAME_TEMPLATE'. Prefixes are
    normalised (letters/digits only) and sorted longest-first so 'expense' is
    tried before 'exp'. Tolerant: malformed lines are skipped, never fatal.
    """
    try:
        from core.models import SiteConfig
        raw = SiteConfig.get().numbered_fund_families or ""
    except Exception:
        return []
    families = []
    for line in raw.splitlines():
        if "=" not in line:
            continue
        left, template = line.split("=", 1)
        template = template.strip()
        if "{n}" not in template:
            continue
        prefixes = []
        for part in left.split(","):
            p = re.sub(r"[^a-z0-9]", "", part.strip().lower())
            if p:
                prefixes.append(re.escape(p))
        if prefixes:
            prefixes.sort(key=len, reverse=True)
            families.append((prefixes, template))
    return families


def campaign_allocate(reference, name, phone):
    """Fallback used only after the normal rules miss. If the reference contains
    one of an active campaign's trigger words, match the payer to a campaign
    member (phone, then unique name) and return that campaign's department.

    Returns (campaign, group, department, status) or (None, "", None, None).
    status is AUTO when a member matched, REVIEW when only the trigger matched
    (so an unrecognised giver still routes to the right fund for review).
    """
    import re
    from giving.models import Campaign
    s = re.sub(r"\s+", "", (reference or "").strip().lower())
    if not s:
        return None, "", None, None
    for camp in Campaign.objects.filter(active=True):
        trigs = camp.trigger_list()
        if not trigs or not any(t in s for t in trigs):
            continue
        m = camp.match_member(name, phone)
        if m:
            # split to the member's subgroup fund (the whole point of campaigns);
            # a member with no group falls back to the campaign's parent fund.
            return camp, (m.group or ""), camp.subgroup_department(m.group), "AUTO"
        return camp, "", camp.department, "REVIEW"
    return None, "", None, None


def reallocate_pending():
    """Re-run allocation rules over the items currently in the review queue
    (credits awaiting allocation), so rules added *after* an import can clear
    them without re-importing. Updates each transaction in place when it now
    resolves to a fund (directly, via a development-group token, or via the
    campaign fallback). Split-fund matches and locked periods are left for
    manual handling. Returns a summary dict.
    """
    from giving.models import Transaction, SplitFund
    from giving.services.allocation import campaign_allocate
    from statements.services.importer import _resolve
    from core.models import entry_blocked

    qs = (Transaction.objects.filter(
            allocation_status=Transaction.Status.REVIEW,
            direction=Transaction.Direction.CREDIT,
            processed_via_envelope=False, manual_receipt=False))

    scanned = allocated = skipped_locked = skipped_split = 0
    for t in qs.iterator():
        scanned += 1
        if entry_blocked(t.service_sabbath or t.date):
            skipped_locked += 1
            continue
        resolver, status = allocate(t.reference, t.date)
        if isinstance(resolver, SplitFund):
            skipped_split += 1          # splitting in place is out of scope here
            continue
        dept, dev_group = _resolve(resolver)
        new_status = None
        campaign = campaign_group = None
        if dept is not None:
            new_status = (Transaction.Status.AUTO if status == "AUTO"
                          else Transaction.Status.LEARNED)
        else:
            campaign, campaign_group, cdept, cstatus = campaign_allocate(
                t.reference, t.payer_name, t.payer_phone)
            if cdept is not None and cstatus == "AUTO":
                dept, new_status = cdept, Transaction.Status.AUTO
        if dept is None or new_status not in (Transaction.Status.AUTO,
                                              Transaction.Status.LEARNED):
            continue
        t.department = dept
        t.dev_group = dev_group
        t.allocation_status = new_status
        fields = ["department", "dev_group", "allocation_status"]
        if campaign is not None:
            t.campaign = campaign
            t.campaign_group = campaign_group or ""
            fields += ["campaign", "campaign_group"]
        t.save(update_fields=fields)
        allocated += 1
    return {"scanned": scanned, "allocated": allocated,
            "remaining": scanned - allocated,
            "skipped_locked": skipped_locked, "skipped_split": skipped_split}
