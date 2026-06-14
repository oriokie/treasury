"""Item 1: reconcile a Sabbath's BANK giving (receipted + manually receipted)
against the ENVELOPE records counted for that Sabbath.

The goal is a balanced view: every bank gift that hit the account should be
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
    this Sabbath. We then greedily pair bank gifts to envelopes by amount + name
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
        # status: receipted if any part is, else manual if any, else unreceipted
        statuses = {("receipted" if p.processed_via_envelope else
                     "manual" if p.manual_receipt else "unreceipted") for p in parts}
        status = ("receipted" if "receipted" in statuses else
                  "manual" if "manual" in statuses else "unreceipted")
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
    matched = []
    bank_left = list(bank)
    env_left = list(envelopes)

    def _take_match(min_ratio):
        for b in list(bank_left):
            best, best_r = None, 0.0
            for e in env_left:
                if e["amount"] != b["amount"]:
                    continue
                r = _ratio(b["who"], e["who"])
                if r > best_r:
                    best, best_r = e, r
            if best is not None and best_r >= min_ratio:
                conf = "exact" if best_r >= 0.999 else "fuzzy"
                matched.append({"bank": b, "env": best, "confidence": conf,
                                "ratio": round(best_r, 2)})
                bank_left.remove(b)
                env_left.remove(best)

    _take_match(0.999)            # exact name + exact amount
    _take_match(fuzzy_threshold)  # fuzzy name + exact amount

    # --- singleton suggestion ------------------------------------------------
    # if names didn't match but exactly one bank gift and one envelope remain
    # unmatched, they are almost certainly the same gift — surface a suggestion.
    suggestion = None
    if len(bank_left) == 1 and len(env_left) == 1:
        suggestion = {"bank": bank_left[0], "env": env_left[0],
                      "same_amount": bank_left[0]["amount"] == env_left[0]["amount"]}

    bank_total = sum((b["amount"] for b in bank), Decimal(0))
    env_total = sum((e["amount"] for e in envelopes), Decimal(0))
    # bank-attributed envelopes are those marked BANK channel
    env_bank_total = sum((e["amount"] for e in envelopes if e["is_bank"]), Decimal(0))
    env_cash_total = env_total - env_bank_total

    return {
        "sabbath": sabbath,
        "bank": bank, "envelopes": envelopes,
        "matched": matched, "bank_unmatched": bank_left, "env_unmatched": env_left,
        "suggestion": suggestion,
        "bank_total": bank_total,
        "bank_receipted": sum((b["amount"] for b in bank if b["status"] == "receipted"), Decimal(0)),
        "bank_manual": sum((b["amount"] for b in bank if b["status"] == "manual"), Decimal(0)),
        "bank_unreceipted": sum((b["amount"] for b in bank if b["status"] == "unreceipted"), Decimal(0)),
        "env_total": env_total, "env_bank_total": env_bank_total,
        "env_cash_total": env_cash_total,
        # the two sides balance when the bank gifts equal the bank-attributed
        # envelopes (cash envelopes are their own money and excluded here)
        "balanced": bank_total == env_bank_total,
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
