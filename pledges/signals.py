"""Keep pledge payment links in sync when a contribution changes.

A ``PledgePayment`` is written when a gift arrives (``handle_new_contribution``)
but the gift can change afterwards — reversed, reallocated to another fund, or
credited to another member. This listener revisits the links whenever a
contribution is *updated* in a way that could affect matching, so the pledge
tracker on ``/pledges/<id>/`` never goes on counting money that has moved.

It runs after the surrounding transaction commits (so it sees the saved row and
cannot break the save), is a no-op on freshly created rows (they have no links
yet — creation paths call ``handle_new_contribution`` directly), and only fires
for saves that touched a field matching actually depends on.
"""
from django.db import transaction as db_tx
from django.db.models.signals import post_save
from django.dispatch import receiver

#: Fields a match verdict can turn on. A save that changed none of these (e.g.
#: ``claimed_by`` while working the review queue) cannot change any link, so it
#: is skipped rather than triggering a needless re-evaluation.
_MATCH_FIELDS = {
    "department", "dev_group", "member", "amount", "reference",
    "payer_name", "payer_phone", "direction", "confirmed",
    "is_reversed", "is_reversal",
}


@receiver(post_save, sender="giving.Transaction",
          dispatch_uid="pledges_resync_on_transaction_change")
def resync_pledges_on_transaction_change(sender, instance, created,
                                         update_fields=None, **kwargs):
    # A brand-new gift has no links to reconcile; its creation path runs
    # matching itself. Only *changes* to an existing gift concern us here.
    if created:
        return
    # A targeted save that touched nothing matching cares about is skipped.
    # A full save (update_fields is None) — e.g. the edit form, or split — is
    # always evaluated, since that is the reallocation / member-change path.
    if update_fields is not None and not (_MATCH_FIELDS & set(update_fields)):
        return

    txn_id = instance.pk

    def _run():
        from giving.models import Transaction
        from pledges.services.matching import resync_contribution_pledges
        txn = Transaction.objects.filter(pk=txn_id).first()
        if txn is not None:
            resync_contribution_pledges(txn)

    db_tx.on_commit(_run)
