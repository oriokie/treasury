"""
Matching contributions to pledges.

Suggestions only by default — money is recognised by the existing giving flow;
this layer proposes which confirmed contributions could fulfil which pledge, and
the treasurer confirms. Nothing here creates or moves money.
"""
import datetime as dt
from decimal import Decimal
from difflib import SequenceMatcher

from django.db.models import Q

from members.services.matching import name_key
from giving.models import Transaction
from pledges.models import Pledge, PledgePayment


def applied_by_transaction():
    """How much of each contribution has already been applied to some pledge."""
    from django.db.models import Sum
    return {row["transaction_id"]: row["t"] or Decimal("0")
            for row in (PledgePayment.objects
                        .filter(transaction__isnull=False)
                        .values("transaction_id")
                        .annotate(t=Sum("amount")))}


def already_matched_txn_ids():
    """Contributions with nothing left to give — applied in FULL to some pledge.

    This used to be every contribution touched by any pledge at all, which
    stranded the remainder of a part-applied gift for good: a member who gave
    10,000 against a 4,000 pledge had 6,000 of their own money made permanently
    invisible to matching, and their pledge went on reading as unpaid however
    much they gave afterwards. A treasurer would then chase somebody who had
    already paid.

    That it was wrong is visible in the code it fed: `suggest_matches_for_pledge`
    computes how much of each candidate is still unapplied, and that
    subtraction could never once have found anything to subtract, because every
    such contribution had already been excluded here.
    """
    applied = applied_by_transaction()
    if not applied:
        return set()
    amounts = dict(Transaction.objects.filter(id__in=applied)
                   .values_list("id", "amount"))
    return {tid for tid, used in applied.items()
            if used >= amounts.get(tid, Decimal("0"))}


def campaign_fund_ids(campaign):
    """The funds a gift must land in to count toward this campaign: its target
    fund and every sub-account under it.

    The subtree is the whole point. A camp meeting appeal names CAMP MEETING as
    its fund, but no gift is ever recorded against it — the money lands in
    CAMP_1 … CAMP_30, the per-group sub-accounts. Comparing against the parent
    id alone therefore excluded every real contribution and let through the
    ones that were never meant for the appeal at all.

    Returns an empty set when the campaign names no fund, which the callers
    read as "this campaign cannot be scoped by fund" — not as "nothing
    matches".
    """
    from departments.models import subtree_ids
    # Memoised on the campaign instance. The bulk sweep asks this once per
    # pledge, and every pledge of one campaign resolves the same subtree — two
    # queries each, for the identical answer.
    cached = getattr(campaign, "_pledge_fund_ids", None)
    if cached is None:
        cached = subtree_ids([campaign.target_department_id])
        campaign._pledge_fund_ids = cached
    return cached


def _gift_is_for_campaign(txn, fund_ids):
    """Whether one contribution counts toward a campaign scoped to `fund_ids`.

    A gift with no fund on it does NOT count. It used to: the test read "if the
    campaign names a fund AND the gift names one, they must agree", so an
    unallocated credit skipped the check entirely and was applied to the
    pledge. Unallocated means nobody has yet said what the money was for, which
    is the opposite of evidence that it was for this appeal.
    """
    if not fund_ids:
        return True                  # campaign names no fund; nothing to scope by
    return txn.department_id in fund_ids


def _name_ratio(a, b):
    """Similarity of two names after the same keying the rest of matching uses."""
    ka, kb = name_key(a or ""), name_key(b or "")
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return SequenceMatcher(None, ka, kb).ratio()


def _fuzzy_threshold(cfg=None):
    """0 disables fuzzy suggestions; otherwise a ratio in (0, 1]."""
    from core.models import SiteConfig
    cfg = cfg or SiteConfig.get()
    raw = getattr(cfg, "pledge_match_fuzzy_threshold", None)
    if raw is None:
        return 0.84
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val if val > 0 else 0.0


#: Cash and envelope receipts are typed by hand; bank credits usually arrive
#: with a member link or an exact payer name. Fuzzy name matching is therefore
#: only offered for these channels — the ones where a near-miss is common and
#: a treasurer still reviews before anything is linked.
_FUZZY_CHANNELS = (Transaction.Channel.CASH, Transaction.Channel.ENVELOPE)


def candidate_contributions(pledge, window_days=400, allow_fuzzy=True, cfg=None):
    """Confirmed contributions from this pledge's member that could be applied to
    it: same member (by FK or name key), within the pledge's active window, in the
    campaign's fund or one of its sub-accounts, not already matched, not
    reversed.

    When ``allow_fuzzy`` is True and the fuzzy threshold is set, unlinked cash
    and envelope receipts whose payer name is close enough to the pledgor are
    also returned — tagged so the preview can show they are near-misses.
    Returns a list of ``{"txn", "match"}`` where match is ``"exact"`` or
    ``"fuzzy"``.
    """
    member = pledge.member
    # From the pledge date, not a week before it. A gift given before the
    # promise was made cannot be payment of that promise — it was giving the
    # member had already done, and counting it toward the pledge credits them
    # twice while making the campaign look further along than it is. The grace
    # window that used to sit here was silently doing exactly that for anyone
    # who gave on the Sabbath and pledged the following week.
    start = pledge.start_date
    end = (pledge.end_date or dt.date.today()) + dt.timedelta(days=window_days)
    qs = (Transaction.objects.filter(direction=Transaction.Direction.CREDIT,
            confirmed=True, is_reversal=False, is_reversed=False,
            date__gte=start, date__lte=end)
          .exclude(id__in=already_matched_txn_ids()))
    # match the member by FK, or by name when the gift wasn't linked
    nk = name_key(member.name)
    qs = qs.filter(Q(member=member) | Q(member__isnull=True, payer_name__isnull=False))
    fund_ids = campaign_fund_ids(pledge.campaign)
    threshold = _fuzzy_threshold(cfg) if allow_fuzzy else 0.0
    out = []
    for t in qs.select_related("department"):
        match = None
        if t.member_id == member.id:
            match = "exact"
        elif nk and name_key(t.payer_name or "") == nk:
            match = "exact"
        elif (threshold > 0
              and t.member_id is None
              and t.channel in _FUZZY_CHANNELS
              and _name_ratio(member.name, t.payer_name) >= threshold):
            match = "fuzzy"
        if not match:
            continue
        if not _gift_is_for_campaign(t, fund_ids):
            continue
        out.append({"txn": t, "match": match})
    # Exact before fuzzy so a clear link is preferred when both exist.
    out.sort(key=lambda r: (0 if r["match"] == "exact" else 1, r["txn"].id))
    return out


def suggest_matches_for_pledge(pledge, allow_fuzzy=True, cfg=None):
    """Return candidate contributions with how much of each is still unapplied.

    The subtraction here is what makes a part-applied gift usable again, so it
    is also what stops one being applied twice: a gift already spent down to
    nothing yields `free` of zero and drops out.
    """
    cands = candidate_contributions(pledge, allow_fuzzy=allow_fuzzy, cfg=cfg)
    applied_by_txn = {}
    for pp in PledgePayment.objects.filter(
            transaction__in=[c["txn"].id for c in cands]):
        applied_by_txn[pp.transaction_id] = (
            applied_by_txn.get(pp.transaction_id, Decimal("0")) + pp.amount)
    rows = []
    for c in cands:
        t = c["txn"]
        free = t.amount - applied_by_txn.get(t.id, Decimal("0"))
        if free > 0:
            rows.append({"txn": t, "free": free, "match": c["match"]})
    return rows


def _member_has_other_outstanding(pledge):
    """Whether this member still has another recognised pledge to fill.

    Surplus (giving beyond a completed promise) stays on that promise only when
    there is nowhere else for it to go. If another pledge is still open, the
    extra belongs there first.
    """
    for p in (Pledge.objects.filter(member_id=pledge.member_id,
                                    status__in=Pledge.RECOGNISED_STATUSES)
              .exclude(pk=pledge.pk)):
        if p.outstanding > 0:
            return True
    return False


def _members_still_owing(plan):
    """Member ids who will still have an open balance after `plan` is applied."""
    planned = {}
    for row in plan:
        if row.get("surplus"):
            continue
        pid = row["pledge"].id
        planned[pid] = planned.get(pid, Decimal("0")) + row["amount"]
    owing = set()
    qs = (Pledge.objects.filter(status__in=Pledge.RECOGNISED_STATUSES)
          .only("id", "member_id", "amount"))
    for p in qs:
        left = p.outstanding - planned.get(p.id, Decimal("0"))
        if left > 0:
            owing.add(p.member_id)
    return owing


def plan_auto_match_pledge(pledge, applied_by_txn=None, max_apply=None,
                           allow_fuzzy=True, cfg=None, allow_surplus=False,
                           surplus_only=False):
    """Propose matches for one pledge without writing. Returns
    [{pledge, txn, amount, match, surplus}, …].

    `applied_by_txn` is a mutable map of contribution_id → already-applied
    amount. Callers that walk several pledges (the bulk preview) pass one map
    so a gift's remainder stays visible to the next pledge — the same way a
    real sweep would consume it.

    Outstanding is filled first. Leftover of a gift is then kept on this
    pledge (`allow_surplus`) only when the member has no other open promise —
    otherwise the remainder is left for those pledges.
    """
    if pledge.status in (Pledge.Status.CANCELLED, Pledge.Status.DRAFT):
        return []
    outstanding = Decimal("0") if surplus_only else pledge.outstanding
    if outstanding <= 0 and not (allow_surplus or surplus_only):
        return []
    if applied_by_txn is None:
        applied_by_txn = applied_by_transaction()
    if allow_surplus and not surplus_only and outstanding <= 0:
        if _member_has_other_outstanding(pledge):
            return []
    rows = []
    applied_total = Decimal("0")

    def _take(t, apply_amt, match, surplus=False):
        nonlocal applied_total
        rows.append({"pledge": pledge, "txn": t, "amount": apply_amt,
                     "match": match, "surplus": surplus})
        applied_by_txn[t.id] = applied_by_txn.get(t.id, Decimal("0")) + apply_amt
        applied_total += apply_amt

    cands = candidate_contributions(pledge, allow_fuzzy=allow_fuzzy, cfg=cfg)
    if outstanding > 0:
        for c in cands:
            if outstanding <= 0:
                break
            t = c["txn"]
            free = t.amount - applied_by_txn.get(t.id, Decimal("0"))
            if free <= 0:
                continue
            apply_amt = min(free, outstanding)
            if max_apply is not None:
                apply_amt = min(apply_amt, max_apply - applied_total)
                if apply_amt <= 0:
                    break
            _take(t, apply_amt, c["match"], surplus=False)
            outstanding -= apply_amt

    do_surplus = surplus_only or (
        allow_surplus and not _member_has_other_outstanding(pledge))
    if do_surplus:
        for c in cands:
            t = c["txn"]
            free = t.amount - applied_by_txn.get(t.id, Decimal("0"))
            if free <= 0:
                continue
            apply_amt = free
            if max_apply is not None:
                apply_amt = min(apply_amt, max_apply - applied_total)
                if apply_amt <= 0:
                    break
            # Same (pledge, txn) already in this plan: top up that row rather
            # than emit a second, so the preview checkbox key stays unique.
            existing = next((r for r in rows if r["txn"].id == t.id), None)
            if existing is not None:
                existing["amount"] += apply_amt
                existing["surplus"] = True
                applied_by_txn[t.id] = applied_by_txn.get(t.id, Decimal("0")) + apply_amt
                applied_total += apply_amt
            else:
                _take(t, apply_amt, c["match"], surplus=True)
    return rows


def plan_auto_match_all(campaign=None, allow_fuzzy=True, cfg=None):
    """Dry-run of the bulk sweep: what auto-match would link, without writing.

    Outstanding pledges are filled first (newest first). Gift remainder then
    goes to any other open pledge of the same member; only when none remain is
    leftover kept on a completed pledge so it still shows on the tracker.
    Returns [{pledge, txn, amount, match, surplus}, …].
    """
    qs = (Pledge.objects.filter(status=Pledge.Status.ACTIVE)
          .select_related("campaign", "member", "campaign__target_department"))
    if campaign:
        qs = qs.filter(campaign=campaign)
    applied_by_txn = applied_by_transaction()
    shared = {}
    plan = []
    for pledge in qs:
        pledge.campaign = shared.setdefault(pledge.campaign_id, pledge.campaign)
        plan.extend(plan_auto_match_pledge(
            pledge, applied_by_txn=applied_by_txn,
            allow_fuzzy=allow_fuzzy, cfg=cfg, allow_surplus=False))

    owing = _members_still_owing(plan)
    surplus_qs = (Pledge.objects.filter(
                    status__in=(Pledge.Status.ACTIVE, Pledge.Status.FULFILLED))
                  .select_related("campaign", "member",
                                  "campaign__target_department"))
    if campaign:
        surplus_qs = surplus_qs.filter(campaign=campaign)
    for pledge in surplus_qs:
        if pledge.member_id in owing:
            continue
        pledge.campaign = shared.setdefault(pledge.campaign_id, pledge.campaign)
        plan.extend(plan_auto_match_pledge(
            pledge, applied_by_txn=applied_by_txn,
            allow_fuzzy=allow_fuzzy, cfg=cfg, surplus_only=True))
    merged = []
    index = {}
    for row in plan:
        key = (row["pledge"].id, row["txn"].id)
        if key in index:
            prev = merged[index[key]]
            prev["amount"] += row["amount"]
            prev["surplus"] = prev.get("surplus") or row.get("surplus")
        else:
            index[key] = len(merged)
            merged.append(row)
    return merged


def apply_planned_matches(rows, user=None):
    """Write PledgePayment links for a plan from `plan_auto_match_*`.

    Re-caps each row against current gift free balance so a stale preview
    cannot over-apply a contribution. Fill rows are also capped at outstanding;
    surplus rows (giving beyond a completed promise, with no other open pledge)
    are not. Returns (pledges_touched, total_applied). Creates links only —
    never money.
    """
    touched = set()
    total = Decimal("0")
    # Outstanding can shrink as we write earlier rows of the same pledge.
    outstanding_left = {}
    free_left = dict(applied_by_transaction())  # tid → already applied in DB
    for row in rows:
        pledge, txn, want = row["pledge"], row["txn"], row["amount"]
        surplus = bool(row.get("surplus"))
        if want <= 0:
            continue
        if pledge.id not in outstanding_left:
            outstanding_left[pledge.id] = pledge.outstanding
        out = outstanding_left[pledge.id]
        already = free_left.get(txn.id, Decimal("0"))
        free = txn.amount - already
        if surplus:
            apply_amt = min(want, free)
        else:
            if out <= 0:
                continue
            apply_amt = min(want, free, out)
        if apply_amt <= 0:
            continue
        # One row per (pledge, contribution) — the model enforces it. So a
        # pledge drawing MORE from a gift it has already partly used tops up
        # the existing row rather than adding a second.
        existing = PledgePayment.objects.filter(
            pledge=pledge, transaction=txn).first()
        if existing is not None:
            existing.amount += apply_amt
            existing.save(update_fields=["amount"])
        else:
            PledgePayment.objects.create(
                pledge=pledge, transaction=txn, amount=apply_amt,
                date=txn.date, source=PledgePayment.Source.AUTO,
                matched_by=user, note="Auto-matched")
        free_left[txn.id] = already + apply_amt
        if not surplus:
            outstanding_left[pledge.id] = out - apply_amt
        touched.add(pledge.id)
        total += apply_amt
    return len(touched), total


def auto_match_pledge(pledge, user=None, max_apply=None, allow_fuzzy=True,
                      allow_surplus=None):
    """Apply candidate contributions to a pledge.

    Fills outstanding first. Leftover is kept on this pledge when the member
    has no other open promise (so extra giving still shows on the tracker).
    Returns the total newly applied. Creates PledgePayment links only — never
    money."""
    if allow_surplus is None:
        allow_surplus = not _member_has_other_outstanding(pledge)
    plan = plan_auto_match_pledge(pledge, max_apply=max_apply,
                                  allow_fuzzy=allow_fuzzy,
                                  allow_surplus=allow_surplus)
    _touched, total = apply_planned_matches(plan, user=user)
    return total


def auto_match_all(user=None, campaign=None, allow_fuzzy=True):
    """Sweep all active, unpaid pledges and auto-match available contributions.
    Returns (pledges_touched, total_applied)."""
    return apply_planned_matches(
        plan_auto_match_all(campaign=campaign, allow_fuzzy=allow_fuzzy),
        user=user)


# ---------------------------------------------------------------------------
# Inline matching hook (Phase 2) — called when a new contribution is created.
# Behaviour is parameterised by SiteConfig.pledge_match_mode:
#   OFF      -> do nothing
#   SUGGEST  -> create a pending PledgeMatchSuggestion for a treasurer to confirm
#   AUTO     -> apply the match immediately (capped at the pledge's outstanding)
# It NEVER moves money — money already moved via the contribution itself.
# ---------------------------------------------------------------------------
def active_pledges_for_contribution(txn, cfg=None, include_fulfilled=False):
    """Pledges this contribution could plausibly fulfil: same member (FK or
    name key, or fuzzy cash/envelope name), contribution dated within the
    pledge window, and — when the setting requires it — the contribution's
    fund matching the campaign's target fund.

    Open pledges only, unless ``include_fulfilled`` so leftover giving can
    still land on a completed promise when the member has nothing else owing.
    """
    from core.models import SiteConfig
    cfg = cfg or SiteConfig.get()
    if txn.direction != Transaction.Direction.CREDIT or not txn.confirmed:
        return []
    if txn.is_reversal or txn.is_reversed:
        return []
    member = txn.member
    nk = name_key(txn.payer_name or "")
    if not member and not nk:
        return []
    statuses = [Pledge.Status.ACTIVE]
    if include_fulfilled:
        statuses.append(Pledge.Status.FULFILLED)
    qs = Pledge.objects.filter(status__in=statuses).select_related(
        "member", "campaign")
    out = []
    fund_cache = {}
    same_fund_only = cfg.pledge_match_same_fund_only
    window = cfg.pledge_match_window_days or 400
    threshold = _fuzzy_threshold(cfg)
    for p in qs:
        # member match
        if member and p.member_id == member.id:
            ok = True
        elif nk and name_key(p.member.name) == nk:
            ok = True
        elif (threshold > 0
              and not member
              and txn.channel in _FUZZY_CHANNELS
              and _name_ratio(p.member.name, txn.payer_name) >= threshold):
            ok = True
        else:
            ok = False
        if not ok:
            continue
        if p.outstanding <= 0 and not include_fulfilled:
            continue
        # date window
        start = p.start_date - dt.timedelta(days=7)
        end = (p.end_date or dt.date.today()) + dt.timedelta(days=window)
        if not (start <= txn.date <= end):
            continue
        # fund match (optional) — the campaign's fund OR any of its
        # sub-accounts, and an unallocated gift is not evidence of intent.
        # Cached per campaign: the sweep runs this for every active pledge, and
        # the pledges of one campaign all resolve the same subtree.
        if same_fund_only:
            camp_id = p.campaign_id
            if camp_id not in fund_cache:
                fund_cache[camp_id] = campaign_fund_ids(p.campaign)
            if not _gift_is_for_campaign(txn, fund_cache[camp_id]):
                continue
        out.append(p)
    return out


def handle_new_contribution(txn, user=None, cfg=None):
    """Entry point called after a contribution is created. Returns a short string
    describing what happened (or None). Safe to call from any create path; never
    raises in a way that would break the contribution itself."""
    from core.models import SiteConfig
    try:
        cfg = cfg or SiteConfig.get()
        mode = cfg.pledge_match_mode
        if mode == SiteConfig.PledgeMatchMode.OFF:
            return None
        pledges = active_pledges_for_contribution(txn, cfg)
        fulfilled = active_pledges_for_contribution(
            txn, cfg, include_fulfilled=True)
        owing = [p for p in pledges if p.outstanding > 0]
        owing.sort(key=lambda p: (-p.outstanding, -p.id))
        if not owing and not fulfilled:
            return None
        if mode == SiteConfig.PledgeMatchMode.AUTO:
            remaining = txn.amount
            applied_total = Decimal("0")
            last = None
            for pledge in owing:
                if remaining <= 0:
                    break
                applied = auto_match_pledge(
                    pledge, user=user, max_apply=remaining, allow_surplus=False)
                remaining -= applied
                applied_total += applied
                last = pledge
            if remaining > 0:
                target = last
                if target is None or _member_has_other_outstanding(target):
                    target = None
                    for p in fulfilled:
                        if p.outstanding <= 0 and not _member_has_other_outstanding(p):
                            target = p
                            break
                if target is not None:
                    applied = auto_match_pledge(
                        target, user=user, max_apply=remaining,
                        allow_surplus=True)
                    applied_total += applied
                    last = target
            if applied_total > 0 and last is not None:
                return (f"auto-applied KES {applied_total:,.0f} to "
                        f"{last.member.name}'s pledge")
            return None
        # SUGGEST: record pending suggestions (deduped), split across open
        # pledges so one gift can fill more than one promise.
        from pledges.models import PledgeMatchSuggestion
        already_applied = (PledgePayment.objects.filter(transaction=txn)
                           .exists())
        if already_applied:
            return None
        remaining = txn.amount
        created_any = False
        last = None
        for pledge in owing:
            if remaining <= 0:
                break
            take = min(remaining, pledge.outstanding)
            if take <= 0:
                continue
            _sug, created = PledgeMatchSuggestion.objects.get_or_create(
                transaction=txn, pledge=pledge, defaults={"amount": take})
            remaining -= take
            created_any = created_any or created
            last = pledge
        if remaining > 0:
            target = last
            if target is None:
                for p in fulfilled:
                    if p.outstanding <= 0 and not _member_has_other_outstanding(p):
                        target = p
                        break
            elif _member_has_other_outstanding(target):
                target = None
            if target is not None:
                sug, created = PledgeMatchSuggestion.objects.get_or_create(
                    transaction=txn, pledge=target,
                    defaults={"amount": remaining})
                if not created:
                    sug.amount += remaining
                    sug.save(update_fields=["amount"])
                created_any = True
                last = target
        if created_any and last is not None:
            return f"flagged a possible match to {last.member.name}'s pledge"
        return None
    except Exception:
        from core.utils import log_exception as _lx; _lx('pledges/services/matching.py')
        # matching is best-effort; never let it break contribution creation
        return None
