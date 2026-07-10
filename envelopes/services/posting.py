"""Canonical envelope-to-ledger posting logic.

Relocated VERBATIM from ``envelopes/views.py`` (the same pattern used earlier
for ``cashbook/services/treasury_position.py``): these are not view code, they
are the one accounting implementation that turns a contributor's entry into a
``giving.Transaction`` — imported by the ledger entry form, the spreadsheet
importer, and now the maker-checker batch poster
(``envelopes/services/batches.py``). ``envelopes.views`` re-imports these
under the same names, so every existing call site keeps working unchanged.

Nothing about any calculation changed in the move — the bodies are identical.
This is also *why* the batch workflow can guarantee "only Post creates ledger
entries, with accounting identical to before": Draft/Review/Approve never call
``_save_envelope``; only ``post_batch`` does, and it calls the exact same
function objects a treasurer's browser has always ultimately triggered.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from core.models import SiteConfig
from core.services.sms import send_receipt_sms
from core.utils import sabbath_of, sabbath_week_of
from departments.models import Department
from giving.models import Transaction

from ..models import Envelope, EnvelopeLine

PREFERRED = ["Tithe", "Combined Offering", "Camp Meeting", "Development",
             "Sabbath School", "Loose Offering", "LCB – Local Church Budget",
             "Thanksgiving Offering"]


def _is_building(name):
    return "building" in (name or "").lower()


def column_catalog(for_import=False):
    """Candidate ledger columns: active funds (excluding Building) + split funds,
    preferred ones first, with sensible defaults pre-selected. When for_import is
    set, sub-accounts (Trust Fund and LCB children) are excluded — imports use the
    standalone funds and split offerings only."""
    from giving.models import SplitFund
    from departments.models import split_component_dept_ids
    skip_ids = split_component_dept_ids() if for_import else set()
    cols = []
    for d in Department.objects.filter(active=True):
        if _is_building(d.name):
            continue
        if d.id in skip_ids:        # the 50% split halves — shown as one split column
            continue
        cols.append({"key": str(d.id), "label": d.name, "name": d.name,
                     "kind": "dept", "trust": d.is_trust})
    for s in SplitFund.objects.filter(active=True):
        cols.append({"key": f"split:{s.id}", "label": f"{s.name} (split)",
                     "name": s.name, "kind": "split", "trust": False})
    pref = [p.lower() for p in PREFERRED]

    def rank(c):
        n = c["name"].lower()
        return (0, pref.index(n)) if n in pref else (1, c["label"].lower())
    cols.sort(key=rank)
    for c in cols:
        c["default"] = c["name"].lower() in set(pref)
    return cols


def _amount(raw):
    try:
        v = Decimal(str(raw).replace(",", "").strip())
        return v if v else None
    except (InvalidOperation, TypeError, AttributeError):
        return None


def _expand_lines(amounts, funds, splits, dev_group=None):
    """amounts: {key: raw}. Returns list of (Department, Decimal[, DevelopmentGroup]),
    expanding splits. If `dev_group` is given it is attached to the Development line."""
    lines = []
    for key, raw in amounts.items():
        amt = _amount(raw)
        if not amt:
            continue
        if str(key).startswith("split:"):
            sf = splits.get(int(str(key).split(":", 1)[1]))
            if sf:
                for pdept, pamt in sf.split(amt):
                    if pamt:
                        lines.append((pdept, pamt))
        else:
            try:
                fid = int(key)
            except (ValueError, TypeError):
                continue
            if fid in funds:
                dept = funds[fid]
                if dev_group is not None and dept.category == "DEVELOPMENT":
                    lines.append((dept, amt, dev_group))
                else:
                    lines.append((dept, amt))
    return lines


def _save_envelope(*, date, name, receipt, channel, lines, member, user, cfg):
    env = Envelope.objects.create(
        date=date, sabbath_week=sabbath_week_of(date), receipt_no=receipt,
        member=member, contributor_name=name,
        channel=(Envelope.Channel.BANK if channel == "BANK" else Envelope.Channel.CASH),
        recorded_by=user)
    svc = sabbath_of(date)   # the Sabbath this gift is counted under
    for line in lines:
        dept, amt = line[0], line[1]
        dev_group = line[2] if len(line) > 2 else None
        # Both cash and bank envelopes post income: the envelope (the offering
        # record) IS the income, exactly as the legacy import did. A bank
        # envelope's matching bank-statement credit is excluded from income
        # during Sabbath reconciliation, so the gift is counted once — on the
        # envelope side — and never double-counted against the bank credit.
        txn = Transaction.objects.create(
            date=date, sabbath_week=env.sabbath_week, service_sabbath=svc,
            channel=Transaction.Channel.ENVELOPE,
            direction=Transaction.Direction.CREDIT, amount=amt,
            department=dept, dev_group=dev_group, member=member, payer_name=name,
            reference=f"envelope {receipt}",
            allocation_status=Transaction.Status.MANUAL,
            raw_narration=f"ENVELOPE {receipt}")
        EnvelopeLine.objects.create(envelope=env, department=dept, amount=amt,
                                    dev_group=dev_group, transaction=txn)
    env.recompute_total()
    env.save(update_fields=["total"])
    send_receipt_sms(env, cfg)
    return env
