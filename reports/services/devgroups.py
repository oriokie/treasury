"""Development-group partitioning — the balance-by-giving-capability algorithm.

Pure algorithm (no ORM, no request), extracted verbatim from reports/views.py.
Behaviour is unchanged; reports/views.py re-exports `_balanced_partition` so the
DevGroupBuilderView keeps calling it exactly as before.
"""
from decimal import Decimal


def balanced_partition(items, n, balance_size=True):
    """Partition members into n development groups balanced by giving capability.

    Two phases:

    1. **Greedy seed** - heaviest giver first into the currently lightest group,
       with a soft size cap (ceil(members / n)) so the groups also end up with an
       even *number* of members, not just even totals. (The previous version
       balanced totals only, which could leave one big giver alone in a group
       while everyone else clustered elsewhere.)

    2. **Local-search refinement** - repeatedly swap a member of the richest
       group with a lighter member of the poorest group whenever doing so shrinks
       the gap between them. Sizes are preserved (a swap is one-for-one), so this
       only redistributes capability to cut the variance between groups.

    Inherent limit: when giving is highly skewed - e.g. one member contributes
    ~90% of all development giving - *no* partition can equalise the totals; that
    member's group is unavoidably heavier. The algorithm still spreads everyone
    else as evenly as possible and keeps group sizes within one member of each
    other. `items` = [(member_id, name, phone, weight), ...].
    """
    items = sorted(items, key=lambda x: x[3], reverse=True)
    m = len(items)
    cap = (-(-m // n)) if (balance_size and n) else None  # ceil(m / n)
    buckets = [{"members": [], "total": Decimal(0)} for _ in range(n)]

    def lightest(respect_cap=True):
        cands = buckets
        if respect_cap and cap is not None:
            open_b = [b for b in buckets if len(b["members"]) < cap]
            if open_b:
                cands = open_b
        return min(cands, key=lambda x: (x["total"], len(x["members"])))

    for mid, name, phone, w in items:
        b = lightest()
        b["members"].append({"id": mid, "name": name, "phone": phone, "weight": w})
        b["total"] += w

    # Phase 2: variance-reducing swaps between the richest and poorest groups.
    for _ in range(2000):
        hi = max(buckets, key=lambda b: b["total"])
        lo = min(buckets, key=lambda b: b["total"])
        if hi is lo or hi["total"] == lo["total"]:
            break
        gap = hi["total"] - lo["total"]
        best = None  # (improvement, hi_member, lo_member)
        for hm in hi["members"]:
            for lm in lo["members"]:
                delta = hm["weight"] - lm["weight"]   # hm to lo, lm to hi
                if delta <= 0:
                    continue
                new_gap = abs(gap - 2 * delta)
                improve = gap - new_gap
                if improve > 0 and (best is None or improve > best[0]):
                    best = (improve, hm, lm)
        if not best:
            break
        _, hm, lm = best
        hi["members"].remove(hm); hi["total"] -= hm["weight"]
        lo["members"].remove(lm); lo["total"] -= lm["weight"]
        hi["members"].append(lm); hi["total"] += lm["weight"]
        lo["members"].append(hm); lo["total"] += hm["weight"]
    return buckets
