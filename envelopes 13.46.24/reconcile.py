"""Item 1: reconcile a Sabbath's BANK giving (receipted + manually receipted)
against the ENVELOPE records counted for that Sabbath.

The goal is a balanced view: every bank contribution that hit the account should be
accounted for as an envelope (or a manual receipt), and the totals on each side
should agree. Where the names don't line up exactly we fall back to fuzzy
matching, so a manual-receipt name typed with a small misspelling still pairs up.
"""
import datetime as dt
from decimal import Decimal
from difflib import SequenceMatcher

from members.models import name_key, mask_phone
from core.utils import sabbath_of


def _ratio(a, b):
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return SequenceMatcher(None, ka, kb).ratio()


def reconcile_sabbath(sabbath, fuzzy_threshold=0.84):
    """Return a structured reconciliation for one Sabbath.

    Bank side: every BANK credit whose service Sabbath is this Sabbath, tagged
    receipted / manual / unreceipted. Envelope side: every Envelope counted for
    this Sabbath. We then greedily pair bank contributions to envelopes by amount + name
    (exact first, then fuzzy), surface what's left over on each side, and report
    whether the two sides balance.
    """
    from giving.models import Transaction
    from envelopes.models import Envelope

    sabbath = sabbath_of(sabbath)

    # --- bank side -----------------------------------------------------------
    # A split gift (e.g. Combined Offering) is posted as several rows sharing a
    # core_ref base (X, X-S1, X-S2 …). For reconciliation we regroup those parts
    # into one bank gift so its full amount lines up with the single envelope the
    # giver was issued — otherwise a 1,000 envelope never matches a 600/400 split.
    bank_qs = (Transaction.objects.filter(
                   channel=Transaction.Channel.BANK,
                   direction=Transaction.Direction.CREDIT,
                   confirmed=True, is_reversal=False, is_reversed=False,
                   service_sabbath=sabbath)
               .select_related("member", "department").order_by("id"))

    def _split_key(t):
        if t.core_ref:
            return ("c", t.core_ref.split("-S")[0])
        if t.mpesa_ref:
            return ("m", t.mpesa_ref.lower(), t.date)
        if t.reference:
            return ("r", t.reference.lower(), t.date)
        return ("id", t.id)

    groups = {}
    order = []
    for t in bank_qs:
        k = _split_key(t)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(t)

    bank = []
    for k in order:
        parts = groups[k]
        head = parts[0]
        total = sum((p.amount for p in parts), Decimal(0))
        # In the legacy model the envelope is the income; a bank credit that has
        # been receipted is EXCLUDED from income (a memo). So a credit is either
        # "receipted" (excluded — counted once, on the envelope side) or "income"
        # (still counted here — a double-count if it also has an envelope).
        excluded = any(p.excluded_from_income for p in parts)
        status = "receipted" if excluded else "income"
        funds = []
        for p in parts:
            if p.department_id and p.department.name not in funds:
                funds.append(p.department.name)
        bank.append({
            "id": head.id, "amount": total,
            "who": head.member.name if head.member_id else (head.payer_name or "—"),
            "phone": mask_phone(head.member.phone if head.member_id else head.payer_phone),
            "reference": head.reference or "", "status": status,
            "fund": ", ".join(funds), "is_split": len(parts) > 1,
        })

    # --- envelope side -------------------------------------------------------
    # Show each envelope's fund allocation (Tithe, Development, …) to aid the
    # treasurer's confirmation of a match.
    env_qs = (Envelope.objects.filter(date=sabbath)
              .prefetch_related("lines__department").order_by("receipt_no"))
    envelopes = []
    for e in env_qs:
        funds = []
        for ln in e.lines.all():
            nm = ln.department.name if ln.department_id else None
            if nm and nm not in funds:
                funds.append(nm)
        envelopes.append({
            "id": e.id, "amount": e.total, "who": e.contributor_name or "—",
            "channel": e.channel, "receipt": e.receipt_no,
            "is_bank": e.channel == Envelope.Channel.BANK,
            "funds": ", ".join(funds),
        })

    # --- match bank gifts to envelopes (amount must agree) -------------------
    from collections import Counter
    matched = []
    bank_left = list(bank)
    env_left = list(envelopes)

    def _auto_match(min_ratio):
        # Match on name + equal amount, but ONLY when the pairing is unambiguous:
        # exactly one envelope is a candidate for this credit and no other credit
        # competes for that envelope. This never mis-pairs duplicates (two givers
        # of the same amount, or a repeated name), which the treasurer can resolve
        # by hand instead.
        for b in list(bank_left):
            cands = [e for e in env_left
                     if e["amount"] == b["amount"] and _ratio(b["who"], e["who"]) >= min_ratio]
            if len(cands) != 1:
                continue
            e = cands[0]
            rivals = [bb for bb in bank_left if bb is not b
                      and bb["amount"] == e["amount"]
                      and _ratio(bb["who"], e["who"]) >= min_ratio]
            if rivals:
                continue
            matched.append({"bank": b, "env": e,
                            "confidence": "exact" if min_ratio >= 0.999 else "fuzzy",
                            "ratio": round(_ratio(b["who"], e["who"]), 2),
                            # the overstating case: this gift was banked AND typed as
                            # a cash envelope (its own ledger entry), while the bank
                            # credit is still unreceipted — so the money is counted
                            # twice until the envelope is moved to bank.
                            # the double-count case in the legacy model: the
                            # envelope posts income AND this bank credit is still
                            # income (not yet excluded) — mark it receipted to fix
                            "miscat": b["status"] == "income"})
            bank_left.remove(b)
            env_left.remove(e)

    _auto_match(0.999)            # exact name + exact amount, unambiguous
    _auto_match(fuzzy_threshold)  # fuzzy name + exact amount, unambiguous

    # --- suggestions ---------------------------------------------------------
    # Surface likely pairs the auto-match left behind, each confirmable with one
    # tick. Two rules, strongest first, and never suggesting the same credit or
    # envelope twice:
    #   (a) an amount that is the only one of its value on both sides;
    #   (b) within one amount, a name token (e.g. a first name like "ADAM")
    #       carried by exactly one remaining credit and one remaining envelope —
    #       so "ADAM KEN" and "ADAM NYAN" of the same amount pair up when there
    #       is only one Adam of that amount on each side.
    from collections import Counter, defaultdict
    suggestions = []
    used_b, used_e = set(), set()

    def _tokens(name):
        return {t for t in name_key(name).split() if len(t) >= 2}

    bank_amt = Counter(b["amount"] for b in bank_left)
    env_amt = Counter(e["amount"] for e in env_left)
    for b in bank_left:                                   # (a) unique amount
        if b["id"] in used_b or bank_amt[b["amount"]] != 1 or env_amt[b["amount"]] != 1:
            continue
        e = next((e for e in env_left if e["amount"] == b["amount"] and e["id"] not in used_e), None)
        if e:
            suggestions.append({"bank": b, "env": e, "same_amount": True,
                                "reason": "only gift of this amount this Sabbath"})
            used_b.add(b["id"]); used_e.add(e["id"])

    by_amt = defaultdict(lambda: {"b": [], "e": []})      # (b) shared name token
    for b in bank_left:
        if b["id"] not in used_b:
            by_amt[b["amount"]]["b"].append(b)
    for e in env_left:
        if e["id"] not in used_e:
            by_amt[e["amount"]]["e"].append(e)
    for amt, grp in by_amt.items():
        btok, etok = defaultdict(list), defaultdict(list)
        for b in grp["b"]:
            for tk in _tokens(b["who"]):
                btok[tk].append(b)
        for e in grp["e"]:
            for tk in _tokens(e["who"]):
                etok[tk].append(e)
        for tk in sorted(set(btok) & set(etok)):
            if len(btok[tk]) != 1 or len(etok[tk]) != 1:
                continue
            b, e = btok[tk][0], etok[tk][0]
            if b["id"] in used_b or e["id"] in used_e:
                continue
            suggestions.append({"bank": b, "env": e, "same_amount": True,
                                "reason": f"shared name '{tk.title()}' and amount"})
            used_b.add(b["id"]); used_e.add(e["id"])

    # backward-compatible single suggestion (first, or the lone leftover pair)
    suggestion = (suggestions[0] if suggestions else
                  ({"bank": bank_left[0], "env": env_left[0],
                    "same_amount": bank_left[0]["amount"] == env_left[0]["amount"]}
                   if len(bank_left) == 1 and len(env_left) == 1 else None))

    bank_total = sum((b["amount"] for b in bank), Decimal(0))
    env_total = sum((e["amount"] for e in envelopes), Decimal(0))
    # bank-attributed envelopes are those marked BANK channel
    env_bank_total = sum((e["amount"] for e in envelopes if e["is_bank"]), Decimal(0))
    env_cash_total = env_total - env_bank_total

    return {
        "sabbath": sabbath,
        "bank": bank, "envelopes": envelopes,
        "matched": matched, "bank_unmatched": bank_left, "env_unmatched": env_left,
        "suggestion": suggestion, "suggestions": suggestions,
        "miscat_count": sum(1 for m in matched if m.get("miscat")),
        "bank_total": bank_total,
        "bank_receipted": sum((b["amount"] for b in bank if b["status"] == "receipted"), Decimal(0)),
        "bank_income": sum((b["amount"] for b in bank if b["status"] == "income"), Decimal(0)),
        "env_total": env_total, "env_bank_total": env_bank_total,
        "env_cash_total": env_cash_total,
        # In the legacy model the envelope is the income and a receipted credit is
        # excluded. The Sabbath is reconciled when no bank credit that matches an
        # envelope is still counted as income — i.e. nothing is double-counted.
        "balanced": all(not m.get("miscat") for m in matched),
        "difference": bank_total - env_bank_total,
    }


def unsabbathed_bank_count(within_days=120):
    """How many recent BANK credits have no service Sabbath set — they can't be
    reconciled to any Sabbath until one is assigned."""
    from giving.models import Transaction
    since = dt.date.today() - dt.timedelta(days=within_days)
    return (Transaction.objects.filter(
                channel=Transaction.Channel.BANK,
                direction=Transaction.Direction.CREDIT,
                confirmed=True, is_reversal=False, is_reversed=False,
                service_sabbath__isnull=True, date__gte=since).count())
