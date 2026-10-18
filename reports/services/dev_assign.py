"""Suggest a development group for an unassigned Development offering.

Looks at the contributor's history over the past year (by member link and by
name), and at active development-campaign member lists. When several groups
appear, the one with the most prior contributions wins; a true tie yields no
suggestion rather than a guess.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

from django.db.models import Count, Q
from django.utils import timezone

from departments.models import Department, DevelopmentGroup
from departments.services.matching import match_existing_dev_group
from giving.models import Campaign, CampaignMember, Transaction
from members.models import name_key


def _since(days=365):
    return timezone.localdate() - dt.timedelta(days=days)


def _names_for(txn):
    """Candidate name strings — attributed member and/or payer."""
    names = []
    if txn.member_id and getattr(txn, "member", None):
        names.append(txn.member.name)
    if txn.payer_name:
        names.append(txn.payer_name)
    seen, out = set(), []
    for n in names:
        k = name_key(n)
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


def _pick(counts, reasons, groups_by_id):
    """``(group|None, reason)`` — highest count wins; ties → no suggestion."""
    if not counts:
        return None, ""
    best_n = max(counts.values())
    tied = [gid for gid, n in counts.items() if n == best_n]
    if len(tied) > 1:
        return None, "ambiguous"
    gid = tied[0]
    g = groups_by_id.get(gid)
    if not g:
        return None, ""
    return g, f"{reasons.get(gid, 'history')} ({best_n})"


def suggest_groups_for_many(txns, *, since=None):
    """``{txn_id: (DevelopmentGroup|None, reason)}`` for a page of unassigned rows.

    Batched so the list view pays a handful of queries, not one history walk
    per row.
    """
    since = since or _since()
    txns = list(txns)
    empty = {t.id: (None, "") for t in txns}
    if not txns:
        return empty

    groups_by_id = {
        g.id: g for g in DevelopmentGroup.objects.filter(active=True)}

    member_ids = {t.member_id for t in txns if t.member_id}
    names_by_txn = {t.id: _names_for(t) for t in txns}
    all_names = {n for names in names_by_txn.values() for n in names}
    keys_by_txn = {
        tid: [name_key(n) for n in names if name_key(n)]
        for tid, names in names_by_txn.items()}
    all_keys = {k for keys in keys_by_txn.values() for k in keys}

    # member_id → Counter(group_id)
    by_member = defaultdict(Counter)
    if member_ids:
        for r in (Transaction.objects.active()
                  .filter(member_id__in=member_ids,
                          direction=Transaction.Direction.CREDIT,
                          confirmed=True, excluded_from_income=False,
                          department__category=Department.Category.DEVELOPMENT,
                          dev_group__isnull=False, date__gte=since)
                  .values("member_id", "dev_group")
                  .annotate(n=Count("id"))):
            if r["dev_group"] and r["n"]:
                by_member[r["member_id"]][r["dev_group"]] += r["n"]

    # name_key → Counter(group_id) from exact name matches on past gifts
    by_name = defaultdict(Counter)
    if all_names:
        q = Q()
        for n in all_names:
            q |= Q(payer_name__iexact=n) | Q(member__name__iexact=n)
        for r in (Transaction.objects.active()
                  .filter(q, direction=Transaction.Direction.CREDIT,
                          confirmed=True, excluded_from_income=False,
                          department__category=Department.Category.DEVELOPMENT,
                          dev_group__isnull=False, date__gte=since)
                  .values("payer_name", "member__name", "dev_group")
                  .annotate(n=Count("id"))):
            if not r["dev_group"] or not r["n"]:
                continue
            for candidate in (r["payer_name"], r["member__name"]):
                k = name_key(candidate or "")
                if k in all_keys:
                    by_name[k][r["dev_group"]] += r["n"]

    # name_key → Counter(group_id) from development campaign lists
    by_campaign = defaultdict(Counter)
    if all_keys:
        camps = Campaign.objects.filter(
            active=True,
            department__category=Department.Category.DEVELOPMENT)
        for cm in (CampaignMember.objects
                   .filter(campaign__in=camps, name_key__in=all_keys)
                   .exclude(group="")
                   .only("name_key", "group")):
            g = match_existing_dev_group(cm.group)
            if g:
                by_campaign[cm.name_key][g.id] += 1

    # member_id → default group from membership (weak fallback)
    member_default = {}
    for t in txns:
        m = getattr(t, "member", None)
        if m and m.dev_group_id and getattr(m.dev_group, "active", True):
            member_default[t.member_id] = m.dev_group_id

    out = {}
    for t in txns:
        counts = Counter()
        reasons = {}
        if t.member_id and t.member_id in by_member:
            for gid, n in by_member[t.member_id].items():
                counts[gid] += n
                reasons[gid] = "past giving"
        for k in keys_by_txn.get(t.id, []):
            for gid, n in by_name.get(k, {}).items():
                counts[gid] += n
                reasons.setdefault(gid, "name history")
            for gid, n in by_campaign.get(k, {}).items():
                counts[gid] += n
                reasons.setdefault(gid, "campaign list")
        if not counts and t.member_id in member_default:
            counts[member_default[t.member_id]] = 1
            reasons[member_default[t.member_id]] = "member's group"
        out[t.id] = _pick(counts, reasons, groups_by_id)
    return out


def suggest_group_for(txn, *, since=None):
    """Single-row convenience wrapper around the batched suggester."""
    return suggest_groups_for_many([txn], since=since).get(txn.id, (None, ""))
