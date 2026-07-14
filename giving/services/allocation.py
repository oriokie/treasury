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

# Fallback spellings, used only if no patterns are configured in the database.
DEV_NUM_RE = re.compile(r"(?:dev(?:e?l?o?p?)?(?:gr(?:ou)?p?|gp|g)?|gr(?:ou)?p|gp)0*(\d+)")
DEV_WORD_RE = re.compile(r"(?:dev(?:elop)?|grp|group|gp)")

# Compiled dev-group patterns, cached in-process and invalidated by a signal when
# a DevGroupPattern is saved/deleted (see giving/signals.py).
_PATTERN_CACHE = {"loaded": False, "numbered": [], "word": []}


def clear_pattern_cache():
    _PATTERN_CACHE["loaded"] = False
    _PATTERN_CACHE["numbered"] = []
    _PATTERN_CACHE["word"] = []


def _dev_patterns():
    """Return ([compiled numbered], [compiled word]) from the configured
    DevGroupPattern rows, falling back to the built-in spellings if none exist."""
    if _PATTERN_CACHE["loaded"]:
        return _PATTERN_CACHE["numbered"], _PATTERN_CACHE["word"]
    numbered, word = [], []
    try:
        from giving.models import DevGroupPattern
        rows = list(DevGroupPattern.objects.filter(enabled=True)
                    .order_by("sort_order", "id"))
    except Exception:
        rows = []
    for r in rows:
        try:
            rx = re.compile(r.pattern)
        except re.error:
            continue
        (numbered if r.kind == "NUMBERED" else word).append(rx)
    if not numbered and not word:
        numbered, word = [DEV_NUM_RE], [DEV_WORD_RE]  # safe fallback
    _PATTERN_CACHE["numbered"] = numbered
    _PATTERN_CACHE["word"] = word
    _PATTERN_CACHE["loaded"] = True
    return numbered, word


def detect_dev_group(s):
    """Given a normalised reference, return ('NUMBER', n), ('WORD', None) or None
    using the configured patterns. Shared by allocate() and the live tester."""
    numbered, word = _dev_patterns()
    for rx in numbered:
        m = rx.search(s)
        if m and m.groups():
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 99:
                return ("NUMBER", n)
    for rx in word:
        if rx.search(s):
            return ("WORD", None)
    return None


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

    hit = detect_dev_group(s)
    if hit and hit[0] == "NUMBER":
        return f"DEV_GROUP_{hit[1]}", "AUTO"

    # church-configured numbered fund families, e.g. EXPENSE<n> -> fund "CAMP_<n>".
    # One config line covers every group; resolves only when that fund exists.
    # Prefixes may be plain words OR /regex/ patterns (for misspellings and
    # variations, e.g. /expen[sc]es?/). The number is captured by a NAMED group
    # so a user pattern containing its own groups can never shift it.
    for prefixes, template in _numbered_fund_families():
        fm = re.search(r"(?:%s)[ _-]*0*(?P<famnum>\d+)" % "|".join(prefixes), s)
        if fm:
            from departments.models import Department
            name = template.replace("{n}", str(int(fm.group("famnum"))))
            dept = Department.objects.filter(name__iexact=name).first()
            if dept:
                return dept, "AUTO"

    # NOTE: the old "extra dev-group prefixes" setting was read here. It built
    # exactly the regex a DevGroupPattern of kind NUMBERED builds, but could not
    # be labelled, ordered, disabled or audited — two places to configure one
    # behaviour, neither able to see the other. Migration 0025 turned anything a
    # church had configured into real patterns, which _dev_patterns() above
    # already reads. There is now one place.

    exact = list(AllocationRule.objects.filter(reference=s, archived=False).select_related(
        "department", "split_fund"))
    rule = _pick(exact, date)
    if rule:
        return _result(rule)

    # pattern rules (starts-with / ends-with / contains): most specific first
    patterns = list(AllocationRule.objects.filter(archived=False).exclude(
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
    if hit and hit[0] == "WORD":
        return "DEV_GROUP_NA", "AUTO"

    return "UNALLOCATED", "REVIEW"


def _numbered_fund_families():
    """Parse SiteConfig.numbered_fund_families into [(prefixes, template), ...].

    Each non-empty line is 'prefix1, prefix2 = NAME_TEMPLATE'. A prefix is
    either a plain word — normalised to letters/digits and matched literally —
    or, wrapped in slashes, a regular expression (e.g. /expen[sc]es?/ or
    /exp\\w{0,4}/) for misspellings and variations the plain list can't cover.
    Regex prefixes are matched against the NORMALISED reference (lowercase,
    punctuation stripped), must compile, and have any capturing groups made
    non-capturing so they can never break the number extraction. Plain prefixes
    are sorted longest-first so 'expense' is tried before 'exp'. Tolerant:
    malformed lines and invalid patterns are skipped, never fatal.
    """
    try:
        from core.models import SiteConfig
        raw = SiteConfig.get().numbered_fund_families or ""
    except Exception:
        from core.utils import log_exception as _lx; _lx('giving/services/allocation.py')
        return []
    families = []
    for line in raw.splitlines():
        if "=" not in line:
            continue
        left, template = line.split("=", 1)
        template = template.strip()
        if "{n}" not in template:
            continue
        plain, regexes = [], []
        for part in left.split(","):
            part = part.strip()
            if len(part) > 2 and part.startswith("/") and part.endswith("/"):
                pattern = part[1:-1].strip()
                if not pattern:
                    continue
                # capturing groups -> non-capturing, so the family matcher's
                # named number group is the only capture (back-references in a
                # user pattern would break, an acceptable trade for safety)
                pattern = re.sub(r"\((?![?])", "(?:", pattern)
                try:
                    re.compile(pattern)
                except re.error:
                    continue          # an invalid pattern never matches
                regexes.append(f"(?:{pattern})")
            else:
                p = re.sub(r"[^a-z0-9]", "", part.lower())
                if p:
                    plain.append(re.escape(p))
        plain.sort(key=len, reverse=True)
        prefixes = plain + regexes
        if prefixes:
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
        # DEV_GROUP_NA means "clearly development, but which group is
        # unknown from the reference text alone" — give a configured
        # campaign's member table a chance to pin down the exact group from
        # the payer's name/phone, same as when dept was never resolved at all.
        dev_group_unknown = (resolver == "DEV_GROUP_NA")
        if dept is None or dev_group_unknown:
            campaign, campaign_group, cdept, cstatus = campaign_allocate(
                t.reference, t.payer_name, t.payer_phone)
            if cdept is not None and (dept is None or cstatus == "AUTO"):
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
