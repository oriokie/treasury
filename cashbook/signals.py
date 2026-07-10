"""Keep a fixed asset's accumulated cost honest when one of the capital expenses
that make it up changes.

Adding to an asset's cost is an explicit action (the "accumulate" button or a
manual edit). These signals only ever REDUCE or ADJUST the cost so it stays
truthful: if a linked capital expense is reclassified as recurrent, unlinked,
reduced or deleted, the asset's cost drops by the right amount. (The accumulate
view links expenses with a bulk .update(), which bypasses these signals, so it
never double-counts.)
"""
from decimal import Decimal

from django.db.models.signals import pre_save, post_delete
from django.dispatch import receiver

from .models import Expense


def _adjust_asset_cost(asset_id, delta):
    if not asset_id or not delta:
        return
    from assets.models import FixedAsset
    a = FixedAsset.objects.filter(pk=asset_id).first()
    if a:
        a.cost = max(Decimal(0), (a.cost or Decimal(0)) + delta)
        a.save(update_fields=["cost"])


@receiver(pre_save, sender=Expense)
def expense_asset_cost_presave(sender, instance, **kwargs):
    # a recurrent expense can never stay attached to an asset
    if instance.expenditure_type != Expense.ExpenditureType.CAPITAL:
        instance.capitalized_asset = None
    if not instance.pk:
        return
    old = (Expense.objects.filter(pk=instance.pk)
           .values("capitalized_asset_id", "amount").first())
    if not old or not old["capitalized_asset_id"]:
        return
    old_aid, old_amt = old["capitalized_asset_id"], old["amount"]
    if instance.capitalized_asset_id != old_aid:
        # link removed (reclassified / unlinked / moved) -> drop from the old asset
        _adjust_asset_cost(old_aid, -old_amt)
    elif instance.amount != old_amt:
        # still on the same asset but the amount changed -> adjust by the delta
        _adjust_asset_cost(old_aid, instance.amount - old_amt)


@receiver(post_delete, sender=Expense)
def expense_asset_cost_postdelete(sender, instance, **kwargs):
    if instance.capitalized_asset_id:
        _adjust_asset_cost(instance.capitalized_asset_id, -instance.amount)
