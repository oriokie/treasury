"""Asset lifecycle: the state machine and its guards (EAM Phase 2c).

One place decides whether a status change is allowed, who may make it, and what
must be true first — so the register, the Kanban board and any future API can
never disagree about the rules.

Two guards carry real weight:

* **Disposal is a document, not a status.** `transition()` may never set
  DISPOSED, because a disposal has to record proceeds, a method and a fund, and
  post its journal (gain/loss, and the asset leaving the control accounts). It
  is done through the disposal flow, which then reports the status through
  `mark_disposed()` — the one door to that status, and deliberately not a
  destination in TRANSITIONS. For a long time the refusal existed and the door
  did not, so DISPOSED was unreachable through the application: a projector
  sold for 300,000 stayed in whatever column it was in, the board's "Disposed"
  column was permanently empty, and — because HELD_SALE leads only back to
  IN_SERVICE and IDLE, both refused once `disposed` is true, while ARCHIVED is
  reachable only from DISPOSED — a sold asset could never be archived off the
  board either.
* **An asset in someone's hands cannot be sent for disposal.** It must be
  checked in first, so the register never writes off something still issued.
  That guard is `check_not_issued`, and it is deliberately a function of its
  own rather than a clause inside `_check`: the two ways an asset can leave the
  register are the HELD_SALE transition and the disposal document, and for a
  long time only the first of them asked. The disposal flow does not go through
  `transition()` at all (see above), so it disposed of assets that were still
  checked out — leaving the register showing something both written off and in
  someone's hands, with an assignment that could never be closed.
"""
import datetime as dt

from django.utils import timezone

from assets.models import FixedAsset, AssetEvent

S = FixedAsset.Status

#: allowed destinations for each status
TRANSITIONS = {
    S.PLANNED:     [S.ON_ORDER, S.IN_CWIP, S.IN_SERVICE, S.ARCHIVED],
    S.ON_ORDER:    [S.IN_CWIP, S.IN_SERVICE, S.PLANNED, S.ARCHIVED],
    S.IN_CWIP:     [S.IN_SERVICE, S.ARCHIVED],
    S.IN_SERVICE:  [S.IDLE, S.MAINTENANCE, S.IMPAIRED, S.HELD_SALE],
    S.IDLE:        [S.IN_SERVICE, S.MAINTENANCE, S.IMPAIRED, S.HELD_SALE],
    S.MAINTENANCE: [S.IN_SERVICE, S.IDLE, S.IMPAIRED, S.HELD_SALE],
    S.IMPAIRED:    [S.IN_SERVICE, S.IDLE, S.HELD_SALE],
    S.HELD_SALE:   [S.IN_SERVICE, S.IDLE],
    S.DISPOSED:    [S.ARCHIVED],
    S.ARCHIVED:    [],
}

#: statuses that mean the asset is commissioned and therefore depreciating
IN_SERVICE_STATES = {S.IN_SERVICE, S.IDLE, S.MAINTENANCE, S.IMPAIRED, S.HELD_SALE}


class TransitionError(Exception):
    """A lifecycle rule refused the change; the message is shown to the user."""


def open_assignment(asset):
    return asset.assignments.filter(to_date__isnull=True).first()


def check_not_issued(asset, before):
    """Refuse to write an asset off while it is still in someone's hands.

    `before` completes the sentence "check it back in before ..." so each caller
    names its own act ("holding it for disposal", "recording a disposal") while
    the rule itself — and the wording of the refusal — stays in one place.
    """
    held = open_assignment(asset)
    if held:
        raise TransitionError(
            f"{asset.name} is still issued to {held.holder}. Check it back in "
            f"before {before}.")
    return True


def allowed_transitions(asset):
    """Destinations this asset may move to right now, guards included."""
    current = asset.status or S.IN_SERVICE
    out = []
    for target in TRANSITIONS.get(current, []):
        try:
            _check(asset, target)
        except TransitionError:
            continue
        out.append(target)
    return out


def _check(asset, target):
    current = asset.status or S.IN_SERVICE
    if target == current:
        raise TransitionError(f"{asset.name} is already {asset.get_status_display().lower()}.")
    if target == S.DISPOSED:
        raise TransitionError(
            "Record a disposal instead — a disposal needs the date, method, proceeds "
            "and the fund, and posts the gain or loss to the ledger.")
    if asset.disposed and target != S.ARCHIVED:
        raise TransitionError(f"{asset.name} has been disposed of and can only be archived.")
    if target not in TRANSITIONS.get(current, []):
        raise TransitionError(
            f"{asset.name} cannot go from {asset.get_status_display().lower()} to "
            f"{FixedAsset.Status(target).label.lower()}.")
    if target == S.HELD_SALE:
        check_not_issued(asset, "holding it for disposal")
    return True


def transition(asset, target, user=None, note="", on=None):
    """Move the asset to `target`, or raise TransitionError.

    Commissioning sets the in-service date if it is missing, because that — not
    the purchase date — is when depreciation starts.
    """
    _check(asset, target)
    previous = asset.get_status_display()
    fields = ["status"]
    asset.status = target
    if target in IN_SERVICE_STATES and not asset.in_service_on:
        asset.in_service_on = on or asset.acquired_on or dt.date.today()
        fields.append("in_service_on")
    asset.save(update_fields=fields)
    summary = f"{previous} → {asset.get_status_display()}"
    if note:
        summary = f"{summary} — {note}"[:200]
    log(asset, AssetEvent.Kind.STATUS, summary, user)
    return asset


def mark_disposed(asset, user=None, note=""):
    """Report a recorded disposal on the register's face: move the asset to
    DISPOSED. The ONLY way that status is ever reached.

    `_check` refuses DISPOSED as a bare status change and must go on refusing
    it — a Kanban drag cannot supply the date, method, proceeds and fund a
    disposal needs, nor post its journal. But a refusal on its own left the
    status unreachable, so the board went on showing a sold asset among the
    ones still waiting to be sold and counted its (nil) book value in that
    column's total.

    So the DOCUMENT reports the status, and it does so here rather than in the
    view, for the same reason every other rule in this module is here: the
    register, the board and anything added later must not each decide for
    themselves what a disposal does to the status.

    Guarded rather than trusting: the row must already carry the disposal —
    `disposed` and `disposed_on` — before the status can say so. That is what
    stops this becoming the back door into DISPOSED that `_check` closes.
    Callers therefore invoke it AFTER saving the disposal and inside the same
    atomic block as the journal, so the register can never be left disposed
    with the board still showing the asset in service.

    Idempotent: recording a disposal twice, or a rebuild replaying one, leaves
    one status and one timeline entry rather than a second.
    """
    if not (asset.disposed and asset.disposed_on):
        raise TransitionError(
            f"{asset.name} has no recorded disposal — record the disposal, with its "
            f"date, method, proceeds and fund, rather than setting the status.")
    if asset.status == S.DISPOSED:
        return asset
    previous = asset.get_status_display()
    asset.status = S.DISPOSED
    # update_fields deliberately narrow: the disposal's own figures were written
    # by the caller's save, and assets.signals._freeze_disposal_figures skips a
    # save that could not persist `disposal_gain_loss` — so this one cannot
    # restate the gain/(loss) the disposal just froze.
    asset.save(update_fields=["status"])
    summary = f"{previous} → {asset.get_status_display()} on {asset.disposed_on:%d %b %Y}"
    if note:
        summary = f"{summary} — {note}"[:200]
    log(asset, AssetEvent.Kind.DISPOSED, summary, user)
    return asset


def log(asset, kind, summary, user=None, at=None):
    """Append to the asset's timeline."""
    return AssetEvent.objects.create(
        asset=asset, kind=kind, summary=summary[:200], actor=user,
        at=at or timezone.now())
