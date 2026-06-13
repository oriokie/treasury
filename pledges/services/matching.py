"""
Matching contributions to pledges.

Suggestions only by default — money is recognised by the existing giving flow;
this layer proposes which confirmed contributions could fulfil which pledge, and
the treasurer confirms. Nothing here creates or moves money.
"""
import datetime as dt
from decimal import Decimal

from django.db.models import Q

from members.services.matching import name_key
from giving.models import Transaction
from pledges.models import Pledge, PledgePayment


def already_matched_txn_ids():
    """Contribution ids already applied to any pledge (so we don't double-count)."""
    return set(PledgePayment.objects.filter(transaction__isnull=False)
               .values_list("transaction_id", flat=True))


def candidate_contributions(pledge, window_days=400):
    """Confirmed contributions from this pledge's member that could be applied to
    it: same member (by FK or name key), within the pledge's active window, in the
    target fund when the campaign names one, not already matched, not reversed."""
    member = pledge.member
    start = pledge.start_date - dt.timedelta(days=7)
    end = (pledge.end_date or dt.date.today()) + dt.timedelta(days=window_days)
    qs = (Transaction.objects.filter(direction=Transaction.Direction.CREDIT,
            confirmed=True, is_reversal=False, is_reversed=False,
            date__gte=start, date__lte=end)
          .exclude(id__in=already_matched_txn_ids()))
    # match the member by FK, or by name key when the gift wasn't linked
    nk = name_key(member.name)
    qs = qs.filter(Q(member=member) | Q(member__isnull=True, payer_name__isnull=False))
    out = []
    for t in qs.select_related("department"):
        if t.member_id == member.id:
            ok = True
        else:
            ok = bool(nk) and name_key(t.payer_name or "") == nk
        if not ok:
            continue
        # if the campaign names a target fund, prefer gifts to that fund
        camp_dept = pledge.campaign.target_department_id
        if camp_dept and t.department_id and t.department_id != camp_dept:
            continue
        out.append(t)
    return out


def suggest_matches_for_pledge(pledge):
    """Return candidate contributions with how much of each is still unapplied."""
    cands = candidate_contributions(pledge)
    applied_by_txn = {}
    for pp in PledgePayment.objects.filter(transaction__in=[c.id for c in cands]):
        applied_by_txn[pp.transaction_id] = applied_by_txn.get(pp.transaction_id, Decimal("0")) + pp.amount
    rows = []
    for t in cands:
        free = t.amount - applied_by_txn.get(t.id, Decimal("0"))
        if free > 0:
            rows.append({"txn": t, "free": free})
    return rows


def auto_match_pledge(pledge, user=None, max_apply=None):
    """Apply candidate contributions to a pledge up to its outstanding balance.
    Used both for one-click "auto-match this pledge" and the bulk sweep. Returns
    the total newly applied. Creates PledgePayment links only — never money."""
    if pledge.status in (Pledge.Status.CANCELLED, Pledge.Status.DRAFT):
        return Decimal("0")
    applied_total = Decimal("0")
    outstanding = pledge.outstanding
    if outstanding <= 0:
        return Decimal("0")
    for row in suggest_matches_for_pledge(pledge):
        if outstanding <= 0:
            break
        apply_amt = min(row["free"], outstanding)
        if max_apply is not None:
            apply_amt = min(apply_amt, max_apply - applied_total)
            if apply_amt <= 0:
                break
        PledgePayment.objects.create(
            pledge=pledge, transaction=row["txn"], amount=apply_amt,
            date=row["txn"].date, source=PledgePayment.Source.AUTO,
            matched_by=user, note="Auto-matched")
        applied_total += apply_amt
        outstanding -= apply_amt
    return applied_total


def auto_match_all(user=None, campaign=None):
    """Sweep all active, unpaid pledges and auto-match available contributions.
    Returns (pledges_touched, total_applied)."""
    qs = Pledge.objects.filter(status=Pledge.Status.ACTIVE)
    if campaign:
        qs = qs.filter(campaign=campaign)
    touched = 0
    total = Decimal("0")
    for pledge in qs:
        applied = auto_match_pledge(pledge, user=user)
        if applied > 0:
            touched += 1
            total += applied
    return touched, total


# ---------------------------------------------------------------------------
# Inline matching hook (Phase 2) — called when a new contribution is created.
# Behaviour is parameterised by SiteConfig.pledge_match_mode:
#   OFF      -> do nothing
#   SUGGEST  -> create a pending PledgeMatchSuggestion for a treasurer to confirm
#   AUTO     -> apply the match immediately (capped at the pledge's outstanding)
# It NEVER moves money — money already moved via the contribution itself.
# ---------------------------------------------------------------------------
def active_pledges_for_contribution(txn, cfg=None):
    """Active, unpaid pledges this contribution could plausibly fulfil: same
    member (FK or name key), gift dated within the pledge window, and — when the
    setting requires it — the gift's fund matching the campaign's target fund."""
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
    qs = Pledge.objects.filter(status=Pledge.Status.ACTIVE).select_related(
        "member", "campaign")
    out = []
    same_fund_only = cfg.pledge_match_same_fund_only
    window = cfg.pledge_match_window_days or 400
    for p in qs:
        # member match
        if member and p.member_id == member.id:
            ok = True
        elif nk and name_key(p.member.name) == nk:
            ok = True
        else:
            ok = False
        if not ok:
            continue
        if p.outstanding <= 0:
            continue
        # date window
        start = p.start_date - dt.timedelta(days=7)
        end = (p.end_date or dt.date.today()) + dt.timedelta(days=window)
        if not (start <= txn.date <= end):
            continue
        # fund match (optional)
        if same_fund_only:
            camp_dept = p.campaign.target_department_id
            if camp_dept and txn.department_id and txn.department_id != camp_dept:
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
        if not pledges:
            return None
        # pick the best single pledge: the one with the largest outstanding that
        # this gift can go toward (most likely the intended one)
        pledge = max(pledges, key=lambda p: p.outstanding)
        if mode == SiteConfig.PledgeMatchMode.AUTO:
            applied = auto_match_pledge(pledge, user=user, max_apply=txn.amount)
            if applied > 0:
                return f"auto-applied KES {applied:,.0f} to {pledge.member.name}'s pledge"
            return None
        # SUGGEST: record a pending suggestion (deduped)
        from pledges.models import PledgeMatchSuggestion
        already_applied = (PledgePayment.objects.filter(transaction=txn)
                           .exists())
        if already_applied:
            return None
        sug, created = PledgeMatchSuggestion.objects.get_or_create(
            transaction=txn, pledge=pledge,
            defaults={"amount": min(txn.amount, pledge.outstanding)})
        if created:
            return f"flagged a possible match to {pledge.member.name}'s pledge"
        return None
    except Exception:
        # matching is best-effort; never let it break contribution creation
        return None
