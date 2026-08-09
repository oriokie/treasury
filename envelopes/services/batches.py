"""Envelope batch workflow service — Draft -> Review -> Approve -> Post.

An ``EnvelopeBatch`` is a staging worksheet; nothing in it touches the ledger
until ``post_batch`` runs, and ``post_batch`` posts by calling
``envelopes.services.posting._save_envelope`` — the exact function the ledger
form and the spreadsheet importer have always used — so posted accounting is
identical to before this workflow existed. Every other function here only
reads/writes ``EnvelopeBatch``/``EnvelopeBatchRow``, which no report, balance
or member-giving figure is computed from.

Validation runs at three points, each a little stricter than the last, because
time passes between them and the world can change underneath a batch (another
batch claims a receipt number, a period gets locked, a fund is deactivated):

* while editing (row-level, for the red-highlight/inline-error UI);
* at Submit and again at Approve (row-level + duplicate receipts + "at least
  one active row" + every money-carrying column still being one the register
  offers);
* at Post (all of that re-run fresh EXCEPT the column check, plus the
  accounting-period lock).

That one asymmetry at Post is deliberate and is the whole of
docs/recommendations.md #63. Deactivating a fund means "no NEW money against
this", so the column check belongs where a row is created or accepted — Submit
and Approve. It must NOT run at Post, because by then the row already exists
and was already approved: re-checking there is what used to make an approved
line vanish (or the batch refuse) over a fund closed in the days between
approval and posting, for cash a treasurer had physically counted. The two
halves are one rule read from both ends — new money can't go in, money already
in doesn't come back out.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction as db_tx

from core.models import SiteConfig, entry_blocked
from departments.models import Department, DevelopmentGroup
from members.models import Member

from .posting import (UnpostableAllocation, _amount, _expand_lines,
                      _save_envelope, column_catalog)
from ..models import Envelope, EnvelopeBatch, EnvelopeBatchRow

TOLERANCE = Decimal("0.01")   # matches CountSession.has_discrepancy elsewhere

# error codes surfaced to the grid, each with the row-level message shown
# beside the offending cell
ERR_TOTAL_MISMATCH = "TOTAL_MISMATCH"
ERR_TOTAL_MISSING = "TOTAL_MISSING"
ERR_NO_RECEIPT = "NO_RECEIPT"
ERR_DUPLICATE_RECEIPT = "DUPLICATE_RECEIPT"
ERR_NO_ALLOCATION = "NO_ALLOCATION"


def recompute_row_total(amounts):
    """Sum of the raw allocation-column values, exactly as typed (pre-split
    expansion — a split fund's later trust/local division must not change
    what the cashier's manual total is compared against)."""
    total = Decimal(0)
    for raw in (amounts or {}).values():
        amt = _amount(raw)
        if amt:
            total += amt
    return total


def row_is_active(contributor_name, amounts):
    """A row counts toward the batch once it has a contributor name — an
    empty trailing grid row (no name at all) is not an error, it's just
    unused. Deliberately NOT also requiring an amount: a row with a name but
    no allocation yet is still "in play" and must be flagged (ERR_NO_
    ALLOCATION) rather than silently dropped at submit time — a cashier who
    typed a name and forgot the amount should see an error, not have that
    contributor's gift quietly vanish."""
    return bool((contributor_name or "").strip())


def validate_row(*, contributor_name, receipt_no, amounts, manual_total):
    """Row-level checks only (no cross-row/cross-batch knowledge — duplicate
    receipts are a batch-level concern, see find_duplicate_receipts). Returns
    (error_code, error_detail, computed_total); error_code is "" when clean."""
    computed = recompute_row_total(amounts)
    if not row_is_active(contributor_name, amounts):
        return "", "", computed
    if not (receipt_no or "").strip():
        return ERR_NO_RECEIPT, "Enter a receipt number.", computed
    if not amounts or computed <= 0:
        return ERR_NO_ALLOCATION, "Enter at least one fund amount.", computed
    if manual_total is None:
        return (ERR_TOTAL_MISSING,
               "Enter the total written on the envelope.", computed)
    if abs(Decimal(manual_total) - computed) >= TOLERANCE:
        return (ERR_TOTAL_MISMATCH,
               f"Envelope total {manual_total:,.2f} doesn't match the "
               f"allocation total {computed:,.2f}.", computed)
    return "", "", computed


def find_duplicate_receipts(batch):
    """Receipt numbers this batch's active rows share with: each other,
    already-POSTED envelopes, or another batch that is still open (any status
    except POSTED/REJECTED — a rejected or already-posted batch's numbers are
    no longer a live claim). Returns {receipt_no: "why"} for every colliding
    receipt number found among this batch's own rows."""
    rows = [r for r in batch.rows.all() if row_is_active(r.contributor_name, r.amounts)
           and (r.receipt_no or "").strip()]
    conflicts = {}
    seen_in_batch = {}
    for r in rows:
        key = r.receipt_no.strip()
        if key in seen_in_batch:
            conflicts[key] = "Used twice in this batch."
        seen_in_batch[key] = r.id

    keys = list(seen_in_batch)
    if not keys:
        return conflicts

    posted = set(Envelope.objects.filter(receipt_no__in=keys)
                .exclude(pk__in=[r.posted_envelope_id for r in rows if r.posted_envelope_id])
                .values_list("receipt_no", flat=True))
    for key in posted:
        conflicts.setdefault(key, "Already used by a posted envelope.")

    other_open = (EnvelopeBatchRow.objects
                  .filter(receipt_no__in=keys)
                  .exclude(batch_id=batch.id)
                  .exclude(batch__status__in=[EnvelopeBatch.Status.POSTED,
                                              EnvelopeBatch.Status.REJECTED])
                  .values_list("receipt_no", "batch_id"))
    for key, other_batch_id in other_open:
        conflicts.setdefault(
            key, f"Already claimed by another open batch (#{other_batch_id}).")
    return conflicts


def revalidate_batch_rows(batch):
    """Re-run row-level + duplicate validation against the batch's CURRENT
    rows and persist the results onto each row (error/error_detail) — the
    single source of truth the grid, the review screen and Submit/Approve/Post
    all read. Returns the list of active rows that are NOT clean."""
    dup = find_duplicate_receipts(batch)
    rows = list(batch.rows.all())
    dirty = []
    for r in rows:
        code, detail, computed = validate_row(
            contributor_name=r.contributor_name, receipt_no=r.receipt_no,
            amounts=r.amounts, manual_total=r.manual_total)
        if not code and row_is_active(r.contributor_name, r.amounts):
            key = (r.receipt_no or "").strip()
            if key in dup:
                code, detail = ERR_DUPLICATE_RECEIPT, dup[key]
        changed = (r.computed_total != computed or r.error != code
                  or r.error_detail != detail)
        r.computed_total, r.error, r.error_detail = computed, code, detail
        if changed:
            r.save(update_fields=["computed_total", "error", "error_detail"])
        if code:
            dirty.append(r)
    return dirty


def _row_and_receipt_problems(batch):
    """The checks EVERY gate shares: row-level validity, duplicate receipts,
    and the batch having something in it at all. Factored out of the two
    validators below because they no longer differ only by addition — Post
    runs this and the period lock but deliberately not the column gate (see
    the module docstring), so Post can no longer be written as "Submit plus
    something"."""
    problems = []
    dirty = revalidate_batch_rows(batch)
    active = [r for r in batch.rows.all()
             if row_is_active(r.contributor_name, r.amounts)]
    if not active:
        problems.append("Add at least one contributor with an amount before "
                        "submitting.")
    for r in dirty:
        label = r.contributor_name or f"Row {r.line_no}"
        problems.append(f"{label} ({r.receipt_no or 'no receipt'}): "
                        f"{r.error_detail}")
    return problems


def _column_names(keys):
    """Display names for raw `amounts` keys ("17", "split:3"), for keys the
    column catalogue no longer offers. The catalogue itself cannot supply
    them — not being in it is the entire point — so the fund and split rows
    are read directly, in one query each rather than one per row. A key naming
    nothing at all keeps its raw form, which is then the most truthful thing
    that can be said about it."""
    keys = {str(k) for k in keys}
    dept_ids = {int(k) for k in keys if k.isdigit()}
    split_ids = {int(k.split(":", 1)[1]) for k in keys
                 if k.startswith("split:") and k.split(":", 1)[1].isdigit()}
    names = {}
    if dept_ids:
        names.update({str(d.id): d.name
                     for d in Department.objects.filter(pk__in=dept_ids)})
    if split_ids:
        from giving.models import SplitFund
        names.update({f"split:{s.id}": s.name
                     for s in SplitFund.objects.filter(pk__in=split_ids)})
    return {k: names.get(k, f"column '{k}'") for k in keys}


def closed_or_unknown_columns(batch):
    """Problems for money entered against a fund column the register does not
    offer — a closed/deactivated fund, an inactive split, or a key naming a
    fund that no longer exists.

    The server-side half of "a deactivated fund cannot be chosen for a NEW
    entry". Until this existed, that rule lived ONLY in the entry grid, which
    builds its columns from `column_catalog()` — true of the browser, and
    trivially untrue of anything else that reaches `autosave_rows`: a stale
    tab whose columns were built before the fund was closed, the spreadsheet
    importer resolving a sheet column onto a fund, or a replayed autosave POST.
    Any of those could seat live money on a closed fund and walk it all the
    way to POSTED, which is precisely the money-in-a-closed-fund the closing
    was meant to prevent.

    Compared against `column_catalog()` and nothing else, on purpose: whatever
    the grid is willing to offer for a new entry is by definition the set of
    columns a new entry may use, so the rule and the UI cannot drift apart.

    Only money-carrying cells are considered. The grid posts a key for every
    open column, most of them blank, and a blank cell against a closed fund is
    not money — the same reason `_expand_lines` skips one. Called from Submit
    and Approve ONLY; see the module docstring for why Post must not.
    """
    offered = {c["key"] for c in column_catalog()}
    flagged = []
    for r in batch.rows.order_by("line_no", "id"):
        if not row_is_active(r.contributor_name, r.amounts):
            continue
        stale = [str(k) for k, raw in (r.amounts or {}).items()
                 if _amount(raw) and str(k) not in offered]
        if stale:
            flagged.append((r, stale))
    if not flagged:
        return []

    names = _column_names({k for _, stale in flagged for k in stale})
    problems = []
    for r, stale in flagged:
        label = r.contributor_name or f"Row {r.line_no}"
        which = ", ".join(names[k] for k in stale)
        # "no longer an open fund column" rather than "is closed": a key that
        # resolves to nothing at all lands here too, and telling a treasurer a
        # fund they cannot find is "closed" sends them looking for something
        # to reopen that was never there.
        verb, noun = (("is", "that amount") if len(stale) == 1
                      else ("are", "those amounts"))
        problems.append(
            f"{label} ({r.receipt_no or 'no receipt'}): {which} {verb} no "
            f"longer an open fund column, and new giving cannot be recorded "
            f"against a closed fund. Move {noun} to an open fund, or reopen "
            f"the fund, then try again.")
    return problems


def validate_batch_for_submit(batch):
    """Problems that must ALL be resolved before Draft/Returned -> Review, and
    re-run at Approve. Returns a list of human-readable strings (empty = ready
    to submit)."""
    return _row_and_receipt_problems(batch) + closed_or_unknown_columns(batch)


def validate_batch_for_post(batch):
    """Everything Submit checks EXCEPT the closed-column gate, re-run fresh,
    plus the accounting-period lock — the last line of defence immediately
    before the ledger is touched. The exclusion is #63 and is load-bearing:
    the row was created and approved while its fund was open, so closing that
    fund afterwards must not strand a Sabbath's counted cash. Post's job is to
    write what was approved, not to re-litigate whether it could have been
    entered today."""
    problems = _row_and_receipt_problems(batch)
    why = entry_blocked(batch.sabbath_date)
    if why:
        problems.append(why)
    return problems


# ===========================================================================
# Autosave — the mutation behind "Draft batches auto-save as you type"
# ===========================================================================

def get_or_create_draft(user, batch_id, sabbath_date):
    """Resolve the batch an autosave call should write to: the given id if it
    is still editable and belongs to this user OR this user has data-entry
    rights (Treasurer/Assistant may continue a colleague's draft — see
    EnvelopeLedgerCreate.get — so the same call must keep saving into THAT
    batch rather than forking a new one), otherwise a fresh DRAFT. Never
    writes into a batch that has moved past editing — that always starts a
    new draft instead, so a stale browser tab can never corrupt a batch
    already sent for review."""
    if batch_id:
        from core import roles
        batch = EnvelopeBatch.objects.filter(pk=batch_id).first()
        if (batch and (batch.created_by_id == user.id or roles.can_enter_data(user))
                and batch.is_editable
                and batch.source == EnvelopeBatch.Source.MANUAL):
            if batch.sabbath_date != sabbath_date:
                batch.sabbath_date = sabbath_date
                batch.save(update_fields=["sabbath_date"])
            return batch, False
    batch = EnvelopeBatch.objects.create(
        sabbath_date=sabbath_date, source=EnvelopeBatch.Source.MANUAL,
        status=EnvelopeBatch.Status.DRAFT, created_by=user)
    return batch, True


def _as_id(value):
    """Coerce a payload id to int, or None. Client JSON always sends form
    values as strings (dev_group_id from a <select>'s .value, member_id from
    a hidden <input>'s .value) — a dict keyed by model .id (always int) never
    matches a string lookup key ("4" != 4 as dict keys), even though the SAME
    string works fine in an ORM pk__in filter (Django coerces it there). This
    silently dropped dev_group on every row despite the group being sent
    correctly — the fix is here, once, rather than trusting every call site
    to remember the coercion."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def autosave_rows(batch, rows_payload):
    """Replace the batch's rows from the client's current grid state and
    re-validate. `rows_payload` is a list of dicts (line_no, receipt_no,
    receipt_no_overridden, contributor_name, member_id, phone, channel,
    dev_group_id, manual_total, amounts). Wholesale replace is simplest and
    entirely safe here — rows are pre-ledger staging data with no history
    worth diffing, and a Sabbath's batch is at most a few dozen rows."""
    member_ids = {_as_id(r.get("member_id")) for r in rows_payload} - {None}
    members = {m.id: m for m in Member.objects.filter(pk__in=member_ids)}
    dg_ids = {_as_id(r.get("dev_group_id")) for r in rows_payload} - {None}
    dev_groups = {g.id: g for g in DevelopmentGroup.objects.filter(pk__in=dg_ids)}

    with db_tx.atomic():
        batch.rows.all().delete()
        new_rows = []
        for i, r in enumerate(rows_payload, start=1):
            amounts = r.get("amounts") or {}
            if not isinstance(amounts, dict):
                amounts = {}
            manual_total = _amount(r.get("manual_total"))
            new_rows.append(EnvelopeBatchRow(
                batch=batch, line_no=i,
                receipt_no=(r.get("receipt_no") or "").strip()[:20],
                receipt_no_overridden=bool(r.get("receipt_no_overridden")),
                contributor_name=(r.get("contributor_name") or "").strip()[:120],
                member=members.get(_as_id(r.get("member_id"))),
                phone=(r.get("phone") or "")[:20],
                channel=r.get("channel") if r.get("channel") in ("CASH", "BANK") else "CASH",
                dev_group=dev_groups.get(_as_id(r.get("dev_group_id"))),
                amounts=amounts, manual_total=manual_total))
        EnvelopeBatchRow.objects.bulk_create(new_rows)
    revalidate_batch_rows(batch)


# ===========================================================================
# Workflow transitions
# ===========================================================================

def submit_batch(batch, user):
    problems = validate_batch_for_submit(batch)
    if problems:
        return problems
    batch.status = EnvelopeBatch.Status.REVIEW
    batch.submitted_by = user
    batch.submitted_at = dt.datetime.now(dt.timezone.utc)
    batch.save(update_fields=["status", "submitted_by", "submitted_at"])
    return []


def approve_batch(batch, user):
    cfg = SiteConfig.get()
    if cfg.require_different_approver and batch.created_by_id == user.id:
        return ["You created this batch — a different treasurer must "
                "approve it (Settings → require a different approver)."]
    problems = validate_batch_for_submit(batch)   # re-check: the world may have moved on
    if problems:
        return problems
    batch.status = EnvelopeBatch.Status.APPROVED
    batch.reviewed_by = user
    batch.reviewed_at = dt.datetime.now(dt.timezone.utc)
    batch.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    return []


def return_batch(batch, user, reason):
    if not (reason or "").strip():
        return ["A reason is required so the batch's creator knows what to fix."]
    batch.status = EnvelopeBatch.Status.RETURNED
    batch.reviewed_by = user
    batch.reviewed_at = dt.datetime.now(dt.timezone.utc)
    batch.return_reason = reason.strip()
    batch.save(update_fields=["status", "reviewed_by", "reviewed_at",
                              "return_reason"])
    return []


def reject_batch(batch, user, reason):
    if not (reason or "").strip():
        return ["A reason is required for the record."]
    batch.status = EnvelopeBatch.Status.REJECTED
    batch.reviewed_by = user
    batch.reviewed_at = dt.datetime.now(dt.timezone.utc)
    batch.reject_reason = reason.strip()
    batch.save(update_fields=["status", "reviewed_by", "reviewed_at",
                              "reject_reason"])
    return []


class _PostingConflict(Exception):
    """Raised — and caught by post_batch itself — when a receipt collides at
    the last possible moment. Raising it inside the atomic block guarantees
    the whole block rolls back (nothing partially posted)."""


def post_batch(batch, user):
    """The ONLY function in this workflow that touches the ledger. Re-validates
    one last time, then — inside one atomic transaction — posts every active
    row via the canonical `_save_envelope`, exactly as the ledger form always
    has. Returns (problems, envelope_count); on any problem nothing is posted
    and the batch stays APPROVED."""
    cfg = SiteConfig.get()
    if cfg.require_different_approver and batch.created_by_id == user.id:
        return (["You created this batch — a different treasurer must "
                 "post it (Settings → require a different approver)."], 0)
    problems = validate_batch_for_post(batch)
    if problems:
        return problems, 0

    # Deliberately NOT filtered to active=True — see docs/recommendations.md
    # #63. Days can pass between a batch being drafted and posted, and a fund
    # deactivated in that window used to make its line vanish at Post: the
    # envelope saved short, or (if it was the row's only fund) as a zero-total
    # envelope with the receipt number consumed and no ledger entry, for cash
    # a treasurer had physically counted and reconciled. Deactivating a fund
    # is meant to stop NEW money being entered against it, and it still does —
    # that gate is `closed_or_unknown_columns`, run at Submit and Approve
    # against `column_catalog()`, the same catalogue the entry grid builds its
    # columns from. It was never meant to un-post an approved batch that
    # already references the fund; that money was given to it before it
    # closed, which is why the gate is not repeated here.
    funds = {d.id: d for d in Department.objects.all()}
    from giving.models import SplitFund
    splits = {s.id: s for s in SplitFund.objects.all()}

    try:
        with db_tx.atomic():
            # lock the batch row to serialise concurrent post attempts on the
            # same batch (two treasurers clicking Post within the same second)
            locked = EnvelopeBatch.objects.select_for_update().get(pk=batch.pk)
            if locked.status != EnvelopeBatch.Status.APPROVED:
                return (["This batch is no longer awaiting posting (someone "
                         "else may have just posted or returned it) — reload "
                         "the page."], 0)
            count = 0
            for row in batch.rows.order_by("line_no", "id"):
                if not row_is_active(row.contributor_name, row.amounts):
                    continue
                if Envelope.objects.filter(receipt_no=row.receipt_no).exists():
                    # final, authoritative check — the DB's own unique
                    # constraint is the ultimate guard, but a friendly message
                    # beats a raw IntegrityError bubbling out of the request
                    raise _PostingConflict(
                        f"Receipt {row.receipt_no} was taken by another "
                        "batch just now — nothing was posted. Return this "
                        "batch, resolve the clash, and try again.")
                member = row.member
                if member is None and row.contributor_name:
                    member = Member.objects.filter(
                        name__iexact=row.contributor_name).first()
                try:
                    lines = _expand_lines(row.amounts, funds, splits,
                                          dev_group=row.dev_group)
                except UnpostableAllocation as exc:
                    # re-raised only to name the row: the message a treasurer
                    # gets has to say WHOSE envelope is stuck, and _expand_lines
                    # sees a bare amounts dict with no idea who it belongs to
                    label = row.contributor_name or f"Row {row.line_no}"
                    raise UnpostableAllocation(
                        f"{label} (receipt {row.receipt_no or 'none'}): {exc} "
                        "Nothing was posted — fix that fund, or return the "
                        "batch and correct the row, then post again."
                    ) from exc
                env = _save_envelope(
                    date=batch.sabbath_date, name=row.contributor_name,
                    receipt=row.receipt_no, channel=row.channel, lines=lines,
                    member=member, user=user, cfg=cfg)
                row.posted_envelope = env
                row.save(update_fields=["posted_envelope"])
                count += 1

            locked.status = EnvelopeBatch.Status.POSTED
            locked.posted_by = user
            locked.posted_at = dt.datetime.now(dt.timezone.utc)
            locked.save(update_fields=["status", "posted_by", "posted_at"])
    except (_PostingConflict, UnpostableAllocation) as exc:
        # the atomic block above already rolled back everything in it —
        # nothing was posted, and the batch is unchanged (still APPROVED).
        # UnpostableAllocation joins _PostingConflict here for the same
        # reason: both mean "this batch cannot be posted correctly right
        # now", and an all-or-nothing refusal with a message beats posting
        # part of a Sabbath's takings.
        return [str(exc)], 0
    return [], count
