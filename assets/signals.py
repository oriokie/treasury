"""Asset documents reach the general ledger at the moment they happen.

Every other kind of document in the system posts on save: ledger/signals.py has
done that for receipts, payments, refunds and cash transfers since the ledger
was built. Assets were the exception, and not by design — `post_acquisition`,
`post_asset_transfer` and `post_disposal` were written, correct and idempotent,
but the only caller outside the tests was `posting.rebuild()`. So an approved
inter-fund transfer moved the asset on the register and posted nothing; a
donated asset appeared on the register with no journal behind it; and both
stayed that way until a treasurer happened to click "Rebuild general ledger".

That is worse than an ordinary gap because the control that exists to catch
register/ledger drift cannot see it: an inter-fund asset transfer is a pure
equity reallocation, so it never touches FIXED_ASSETS or ACCUM_DEPRECIATION —
the only two accounts `register_vs_ledger` compares. The reconciliation reads
0.00 the whole time the journal is missing.

WHERE EACH ONE POSTS, AND WHY IT DIFFERS

* **Acquisitions post from here, on save.** An acquisition is recorded from at
  least three places (the asset form, capitalising a payment, the spreadsheet
  import), and only one kind of it — a donation — posts a journal at all.
  Hanging the rule on the model's save is what stops the fourth place that
  records one from quietly forgetting to post it.
* **Transfers and disposals post from their view**, not from a signal, because
  the state that must post is not the state that gets saved. An AssetTransfer is
  saved when it is REQUESTED and again when it is decided; only an approval that
  changes fund posts. A FixedAsset is saved every time anyone edits it. A
  post_save receiver would fire on all of that and have to filter it back out,
  and — the part that matters — it could not make the register change and the
  journal succeed or fail together, which is precisely the guarantee those two
  actions need.
"""
from decimal import Decimal

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver


def post_to_ledger(post, obj):
    """Run one of `ledger.services.posting`'s document posters NOW.

    Two things live here so that no caller has to remember them:

    * **Only when the ledger is in use.** A church that has never opened the
      general ledger has no chart of accounts, and posting into that is neither
      possible nor wanted — the first rebuild will bring everything in. This is
      the same `chart_ready()` gate ledger/signals.py applies to receipts and
      payments.
    * **Never quietly.** Exceptions are deliberately NOT caught. Each caller
      wraps the register change and this call in one atomic block, so a journal
      that cannot be written takes the register change down with it and the
      treasurer is told the action was refused. Swallowing the error here would
      recreate, silently, exactly the drift this module exists to end.
    """
    from ledger.services import posting
    if not posting.chart_ready():
        return None
    return post(obj)


@receiver(pre_save, sender="assets.FixedAsset")
def _freeze_disposal_figures(sender, instance, **kwargs):
    """When a row BECOMES disposed, work out its gain/(loss) here, whoever is
    saving it.

    The stored `disposal_gain_loss` stopped being a convenience the moment the
    ledger began reading it back (see
    assets.services.depreciation.accumulated_at_disposal): it now decides what
    the disposal journal posts, what the disposals report shows and what the
    Income & Expenditure statement reports. A derived figure that important
    cannot be left to whichever code path happens to write the row — especially
    when getting it wrong is as easy as calling `net_book_value()` one line too
    late, which is precisely how three fixtures in this repository came to
    record the entire proceeds of a sale as its gain.

    Only the transition matters. A row that is ALREADY disposed is left exactly
    as it is — recomputing on every subsequent save would put back the drift
    this exists to remove — and so is a row created disposed in a single INSERT
    (an import, a restored backup, an opening-balance load), which is carrying
    history we have no business restating.
    """
    from assets.models import FixedAsset
    from assets.services import depreciation as dep
    if not (instance.pk and instance.disposed and instance.disposed_on):
        return
    fields = kwargs.get("update_fields")
    if fields is not None and "disposal_gain_loss" not in fields:
        return                      # this save could not persist it anyway
    was_disposed = (FixedAsset.objects.filter(pk=instance.pk)
                    .values_list("disposed", flat=True).first())
    if was_disposed is None or was_disposed:
        return                      # brand new, or disposed already: not the recording
    instance.disposal_gain_loss = dep.gain_or_loss_on_disposal(
        instance, instance.disposed_on, Decimal(instance.disposal_proceeds or 0))


@receiver(post_save, sender="assets.Acquisition")
def _acquisition_saved(sender, instance, **kwargs):
    """Post a recorded acquisition. Only a donation actually writes a journal —
    `post_acquisition` decides that, and re-posting replaces its own entry, so a
    later rebuild lands on the same single entry rather than a second one."""
    from ledger.services import posting
    post_to_ledger(posting.post_acquisition, instance)
