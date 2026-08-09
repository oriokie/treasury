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

# Aliased on import, never bare. Django's ValidationError is what a
# misconfigured SplitFund throws out of `split()`, and this module already
# defines its own posting-refusal exception a line-scan away
# (`UnpostableAllocation`); a bare `ValidationError` here reads as "ours" and
# that is exactly the confusion that let the split case go uncaught.
from django.core.exceptions import ValidationError as DjangoValidationError

from core.models import SiteConfig
from core.services.sms import send_receipt_sms
from core.utils import sabbath_of, sabbath_week_of
from departments.models import Department
from giving.models import Transaction

from ..models import Envelope, EnvelopeLine

PREFERRED = ["Tithe", "Combined Offering", "Camp Meeting", "Development",
             "LCB – Local Church Budget"]


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


def _shape_subgroups(children):
    """Order and label a fund's sub-account children.

    The ordering rule lives here alone so the single-fund path
    (`subgroups_for`) and the whole-register path (`column_catalog`) cannot
    disagree about what a subgroup list looks like. They differ only in how the
    children are fetched — one query for one fund, or one query for all of them.
    """
    out = [{"id": k.id, "label": k.name, "number": _trailing_number(k.name)}
           for k in children]
    out.sort(key=lambda r: (r["number"] is None, r["number"] or 0, r["label"]))
    return out


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

    One query. For every fund at once, use `column_catalog`, which fetches the
    whole set in one go rather than calling this in a loop.
    """
    if dept.category == Department.Category.DEVELOPMENT:
        return []
    return _shape_subgroups(dept.subgroups.filter(active=True).order_by("name"))


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
    funds = list(Department.objects.filter(active=True))
    # Every fund's children in one query, grouped by parent. This used to call
    # subgroups_for(d) inside the loop below — one query per fund, so the
    # envelope grid and template cost two queries for every fund on the
    # register and grew with it (148 queries at 79 funds). The ordering rule is
    # unchanged: both paths go through _shape_subgroups.
    children = {}
    for kid in Department.objects.filter(active=True, parent__in=funds).order_by("name"):
        children.setdefault(kid.parent_id, []).append(kid)
    cols = []
    for d in funds:
        if _is_building(d.name):
            continue
        if d.id in skip_ids:        # the 50% split halves — shown as one split column
            continue
        # Development funds keep their own group mechanism and never carry
        # subgroups here — the same exclusion subgroups_for applies.
        subs = ([] if d.category == Department.Category.DEVELOPMENT
                else _shape_subgroups(children.get(d.id, [])))
        cols.append({"key": str(d.id), "label": d.name, "name": d.name,
                     "kind": "dept", "trust": d.is_trust,
                     "is_development": d.category == Department.Category.DEVELOPMENT,
                     "subgroups": subs})
    for s in SplitFund.objects.filter(active=True):
        cols.append({"key": f"split:{s.id}", "label": f"{s.name} (split)",
                     "name": s.name, "kind": "split", "trust": False,
                     "is_development": False, "subgroups": []})
    # Which columns open by default, and in what order. A church that collects
    # under different headings than PREFERRED names had no way to say so: every
    # new sheet opened on the wrong columns and someone re-picked them by hand,
    # every Sabbath. Configured keys win; PREFERRED is the fallback for a church
    # that has not said otherwise, so behaviour is unchanged until it does.
    chosen = configured_default_keys()
    if chosen:
        rank_of = {key: i for i, key in enumerate(chosen)}

        def rank(c):
            return ((0, rank_of[c["key"]]) if c["key"] in rank_of
                    else (1, c["label"].lower()))
        cols.sort(key=rank)
        for c in cols:
            c["default"] = c["key"] in rank_of
        return cols

    pref = [p.lower() for p in PREFERRED]

    def rank(c):
        n = c["name"].lower()
        return (0, pref.index(n)) if n in pref else (1, c["label"].lower())
    cols.sort(key=rank)
    for c in cols:
        c["default"] = c["name"].lower() in set(pref)
    return cols


def configured_default_keys():
    """The church's chosen default columns, in order, or [] if it has not set any.

    Returns [] rather than raising on anything unreadable: this is called to
    build the entry grid, and a broken setting must not be able to stop a
    Sabbath's envelopes being entered.
    """
    try:
        from core.models import SiteConfig
        raw = SiteConfig.get().envelope_default_funds or ""
    except Exception:      # noqa: BLE001
        return []
    seen, out = set(), []
    for line in raw.replace(",", "\n").splitlines():
        key = line.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


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


class UnpostableAllocation(Exception):
    """A nonzero amount on a row could not be turned into ledger lines that
    account for every cent of it, so nothing is posted at all.

    This exists because of what ``_expand_lines`` used to do instead: `continue`.
    A key that did not resolve to a fund was skipped in silence — the envelope
    was still created, the receipt number was still consumed, and the money a
    treasurer had physically counted simply was not in the ledger, with nothing
    anywhere saying so. If it was the row's only fund, `recompute_total()` then
    produced a ZERO-TOTAL envelope against a real receipt (see
    docs/recommendations.md #63). Every total downstream still reconciled,
    which is precisely what made it invisible.

    Refusing to post is always the better failure here: an envelope that will
    not post is a phone call, an envelope that posts short is a hole in the
    accounts nobody finds. `post_batch` catches this, rolls the whole batch
    back and tells the treasurer which row to fix.
    """


def _int_or_none(value):
    """The int a fund key names, or None if it names no integer at all.
    `amounts` keys arrive as JSON strings ("17", "split:3"), and a key that is
    not a number must fall through to the caller's "resolved nothing" branch
    rather than exploding with a ValueError halfway through a posting run."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _expand_lines(amounts, funds, splits, dev_group=None):
    """amounts: {key: raw}. Returns list of (Department, Decimal[, DevelopmentGroup]),
    expanding splits. If `dev_group` is given it is attached to the Development line.

    The invariant, enforced once here for every caller: each nonzero amount
    comes out the other side as lines summing to EXACTLY it, or nothing posts
    and `UnpostableAllocation` is raised. That single check — rather than a
    guard bolted onto each way a key can fail to resolve — is what closes the
    whole class of the bug in #63, not just the deactivated-fund path that
    exposed it. A fund id that no longer exists, a `split:` key naming a
    deleted split, a key that is not a number at all, a split fund configured
    with no components, and a split fund whose component percentages do not
    total 100 are all the same failure and all raise the same exception — the
    last of those used to escape as a django ValidationError instead, i.e. a
    500 rather than a refusal anyone could act on.

    A zero, blank or unparseable amount is still skipped, and deliberately so:
    it creates no line because there is nothing to post, and it cannot hide
    money either, because `recompute_row_total` parses the cell with this very
    same `_amount` — so a cell the poster reads as nothing is a cell the
    envelope-total check already read as nothing, and any real figure typed
    there surfaces as a TOTAL_MISMATCH long before Post.
    """
    lines = []
    for key, raw in amounts.items():
        amt = _amount(raw)
        if not amt:
            continue
        if str(key).startswith("split:"):
            sf = splits.get(_int_or_none(str(key).split(":", 1)[1]))
            # A split whose components don't total 100% refuses to divide at
            # all rather than let the last component silently absorb the
            # difference (see SplitFund.split — money moved to a fund the
            # church did not choose is the worst error this system can make).
            # That refusal is right; the way it left here was not. It arrives
            # as *django's* ValidationError, which post_batch does not catch,
            # so a 40/40 split used to turn Post into a 500: no problem list,
            # no row named, and a treasurer left unsure whether the batch had
            # gone in. It is the same failure as every other column that
            # cannot be turned into lines summing to the whole, so it is
            # reported the same way — the all-or-nothing refusal below.
            try:
                expanded = ([(pd, pa) for pd, pa in sf.split(amt) if pa]
                            if sf is not None else [])
            except DjangoValidationError as exc:
                raise UnpostableAllocation(
                    f"{amt:,.2f} was allocated to fund column '{key}', but "
                    f"that split cannot divide it: "
                    f"{' '.join(exc.messages)}"
                ) from exc
        else:
            dept = funds.get(_int_or_none(key))
            if dept is None:
                expanded = []
            elif dev_group is not None and dept.category == "DEVELOPMENT":
                expanded = [(dept, amt, dev_group)]
            else:
                expanded = [(dept, amt)]
        allocated = sum((ln[1] for ln in expanded), Decimal(0))
        if allocated != amt:
            raise UnpostableAllocation(
                f"{amt:,.2f} was allocated to fund column '{key}', but only "
                f"{allocated:,.2f} of that can be posted — the column no "
                f"longer resolves to a fund (a deleted fund, or a split fund "
                f"with no components set up). Posting anyway would have "
                f"quietly swallowed the difference.")
        lines.extend(expanded)
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
