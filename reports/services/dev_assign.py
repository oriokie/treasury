"""Suggest a development group for an unassigned Development offering.

Looks at the contributor's history over the past year (by member link and by
name — including near matches such as titles and middle initials), and at
active development-campaign member lists. When several groups appear, the one
with the most prior contributions wins; a true tie yields no suggestion.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

from django.db.models import Count, Q
from django.utils import timezone

from departments.models import Department, DevelopmentGroup
from departments.services.matching import match_existing_dev_group
from giving.models import Campaign, CampaignMember, Transaction
from members.models import name_core_tokens, name_key


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


def _token_index(names_by_txn):
    """token → [(txn_id, core_tokens)] for near-match lookups."""
    idx = defaultdict(list)
    for tid, names in names_by_txn.items():
        seen = set()
        for n in names:
            toks = name_core_tokens(n)
            key = frozenset(toks)
            if len(toks) < 2 or key in seen:
                continue
            seen.add(key)
            for t in toks:
                idx[t].append((tid, toks))
    return idx


def _tokens_near(a, b):
    """Subset match on core token sets (same rule as ``names_near_match``)."""
    if len(a) < 2 or len(b) < 2:
        return bool(a) and a == b
    return a <= b or b <= a


def _near_txn_ids(candidate_name, token_index):
    """Txn ids whose page names near-match ``candidate_name``."""
    ht = name_core_tokens(candidate_name or "")
    if len(ht) < 2:
        return ()
    hits = set()
    for t in ht:
        for tid, pt in token_index.get(t, ()):
            if tid not in hits and _tokens_near(ht, pt):
                hits.add(tid)
    return hits


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
    token_index = _token_index(names_by_txn)

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

    # Per-txn name/history counts (exact + near)
    by_txn_name = defaultdict(Counter)
    if all_names or token_index:
        for r in (Transaction.objects.active()
                  .filter(direction=Transaction.Direction.CREDIT,
                          confirmed=True, excluded_from_income=False,
                          department__category=Department.Category.DEVELOPMENT,
                          dev_group__isnull=False, date__gte=since)
                  .filter(Q(payer_name__gt="") | Q(member__isnull=False))
                  .values("payer_name", "member__name", "dev_group")
                  .annotate(n=Count("id"))):
            if not r["dev_group"] or not r["n"]:
                continue
            seen_keys = set()
            for candidate in (r["payer_name"], r["member__name"]):
                if not candidate:
                    continue
                k = name_key(candidate)
                if k and k in seen_keys:
                    continue
                if k:
                    seen_keys.add(k)
                if k in all_keys:
                    for tid, keys in keys_by_txn.items():
                        if k in keys:
                            by_txn_name[tid][r["dev_group"]] += r["n"]
                else:
                    for tid in _near_txn_ids(candidate, token_index):
                        by_txn_name[tid][r["dev_group"]] += r["n"]

    # Per-txn campaign-list counts (exact name_key + near)
    by_txn_campaign = defaultdict(Counter)
    camps = Campaign.objects.filter(
        active=True,
        department__category=Department.Category.DEVELOPMENT)
    if camps.exists() and (all_keys or token_index):
        for cm in (CampaignMember.objects
                   .filter(campaign__in=camps)
                   .exclude(group="")
                   .only("name", "name_key", "group")):
            g = match_existing_dev_group(cm.group)
            if not g:
                continue
            matched_txns = set()
            if cm.name_key and cm.name_key in all_keys:
                for tid, keys in keys_by_txn.items():
                    if cm.name_key in keys:
                        matched_txns.add(tid)
            else:
                matched_txns.update(_near_txn_ids(cm.name, token_index))
            for tid in matched_txns:
                by_txn_campaign[tid][g.id] += 1

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
        for gid, n in by_txn_name.get(t.id, {}).items():
            counts[gid] += n
            reasons.setdefault(gid, "name history")
        for gid, n in by_txn_campaign.get(t.id, {}).items():
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
