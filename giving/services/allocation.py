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


def _pick(rules, date):
    """From candidate rules, choose the best one for `date`: a period-specific rule
    that covers the date wins over a permanent rule; otherwise the permanent rule."""
    covering = [r for r in rules if r.covers(date)]
    if not covering:
        return None
    period = [r for r in covering if r.is_period]
    if period:
        # narrowest / most recently-starting period first
        period.sort(key=lambda r: (r.valid_from or _MIN, r.valid_to or _MAX), reverse=True)
        return period[0]
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
    patterns.sort(key=lambda r: (order.get(r.match_type, 3), -len(r.reference)))
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
