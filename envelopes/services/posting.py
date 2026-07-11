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


def _trailing_number(name):
    """The trailing integer in a name, e.g. 'Small Group 7' -> 7, 'Group_03' ->
    3 — the same "number a fund by its trailing digits" convention the ledger's
    receipt-sequencing and the numbered-fund-family bank-narration matching
    both already use. None if the name has no trailing digits."""
    import re
    m = re.search(r"(\d+)\s*$", (name or "").strip())
    return int(m.group(1)) if m else None


def subgroups_for(dept):
    """A fund's own sub-account children (Department.parent — e.g. Trust Fund
    → Tithe, Camp Meeting, ...; or any fund set up with numbered subgroups,
    e.g. 'Small Group 1'..'Small Group 12'), as
    [{"id", "label", "number"}, ...] ordered by their trailing number where
    they have one, then by name. Empty for a fund with no subgroups — the
    common case.

    Always empty for a Development-category fund, however it's set up:
    Development already has its own, separately-established group mechanism
    (the DevelopmentGroup tag model — see the ledger's per-column
    ``is_development`` handling) that many other parts of the app already key
    off ``Department.category == DEVELOPMENT`` for (the cash-entry form, the
    review queue's resolve action, the bank importer). This generic
    subgroup-picker is a *different* mechanism (it re-targets the posting
    department itself) and must never engage for a Development fund even if
    that fund happens to also have Department.parent children for some other
    reason — Development keeps exactly its established behaviour.
    """
    if dept.category == Department.Category.DEVELOPMENT:
        return []
    kids = list(dept.subgroups.filter(active=True).order_by("name"))
    out = []
    for k in kids:
        out.append({"id": k.id, "label": k.name, "number": _trailing_number(k.name)})
    out.sort(key=lambda r: (r["number"] is None, r["number"] or 0, r["label"]))
    return out


def column_catalog(for_import=False):
    """Candidate ledger columns: active funds (excluding Building) + split funds,
    preferred ones first, with sensible defaults pre-selected. When for_import is
    set, sub-accounts (Trust Fund and LCB children) are excluded — imports use the
    standalone funds and split offerings only.

    Each column carries ``is_development`` (True when that fund's own
    ``category`` is DEVELOPMENT) so the entry grid can offer the Development-
    Group tag picker for it — mirroring the SAME per-department check the
    cash-entry form, the review queue's resolve action and the bank importer
    already use (``dept.category == Department.Category.DEVELOPMENT``),
    rather than assuming there is exactly one "the" Development fund. A
    church can have more than one fund categorised DEVELOPMENT (e.g. several
    active building/project funds); each gets its own independent picker.

    Any fund that itself has active sub-account children (``subgroups``
    below) carries that list, so the entry grid can offer a "which
    subgroup?" picker for it — the same idea Development Groups provide,
    generalised to any fund set up with real child funds (Department.parent).
    Development funds never carry subgroups here (see subgroups_for) — they
    keep exactly their own established mechanism.
    """
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
                     "kind": "dept", "trust": d.is_trust,
                     "is_development": d.category == Department.Category.DEVELOPMENT,
                     "subgroups": subgroups_for(d)})
    for s in SplitFund.objects.filter(active=True):
        cols.append({"key": f"split:{s.id}", "label": f"{s.name} (split)",
                     "name": s.name, "kind": "split", "trust": False,
                     "is_development": False, "subgroups": []})
    pref = [p.lower() for p in PREFERRED]

    def rank(c):
        n = c["name"].lower()
        return (0, pref.index(n)) if n in pref else (1, c["label"].lower())
    cols.sort(key=rank)
    for c in cols:
        c["default"] = c["name"].lower() in set(pref)
    return cols


def rekey_to_subgroups(amounts, group_number, funds):
    """Given a row's raw amounts ({dept_id_or_split: raw}) and a parsed group
    number (from a sheet's "Group"/"Group Number" column, or the ledger's
    Development-Group-style picker generalised to any fund), reattribute any
    amount whose fund has a subgroup numbered exactly that to the subgroup
    instead of the parent fund — "use the same row allocate" a numbered fund
    family already gets, applied per-row at entry time rather than by parsing
    a bank narration. Amounts for funds with no matching numbered subgroup
    (including funds with no subgroups at all) are left keyed to the fund
    itself, unchanged. Returns a NEW dict; does not mutate the input."""
    if group_number is None:
        return dict(amounts)
    out = {}
    for key, raw in amounts.items():
        dept = funds.get(int(key)) if str(key).isdigit() else None
        if dept is not None:
            for sg in subgroups_for(dept):
                if sg["number"] == group_number:
                    out[str(sg["id"])] = raw
                    break
            else:
                out[key] = raw
        else:
            out[key] = raw
    return out


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
