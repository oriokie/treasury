"""Asset lifecycle: the state machine and its guards (EAM Phase 2c).

One place decides whether a status change is allowed, who may make it, and what
must be true first — so the register, the Kanban board and any future API can
never disagree about the rules.

Two guards carry real weight:

* **Disposal is a document, not a status.** Nothing here may set DISPOSED,
  because a disposal has to record proceeds, a method and a fund, and post its
  journal (gain/loss, and the asset leaving the control accounts). It is done
  through the disposal flow, which then reports the status itself.
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


def log(asset, kind, summary, user=None, at=None):
    """Append to the asset's timeline."""
    return AssetEvent.objects.create(
        asset=asset, kind=kind, summary=summary[:200], actor=user,
        at=at or timezone.now())
