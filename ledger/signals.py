from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver


def _ready():
    from ledger.services import posting
    return posting.chart_ready()


@receiver(post_save, sender="giving.Transaction")
def _txn_saved(sender, instance, **kwargs):
    if _ready():
        from ledger.services import posting
        posting.post_transaction(instance)


@receiver(post_save, sender="cashbook.Expense")
def _exp_saved(sender, instance, **kwargs):
    if _ready():
        from ledger.services import posting
        posting.post_expense(instance)


@receiver(post_delete, sender="giving.Transaction")
def _txn_deleted(sender, instance, **kwargs):
    from ledger.models import JournalEntry
    JournalEntry.objects.filter(source_type="transaction", source_id=instance.pk).delete()


@receiver(post_delete, sender="cashbook.Expense")
def _exp_deleted(sender, instance, **kwargs):
    from ledger.models import JournalEntry
    JournalEntry.objects.filter(source_type="expense", source_id=instance.pk).delete()


@receiver(post_save, sender="cashbook.FundTransfer")
def _transfer_saved(sender, instance, **kwargs):
    if _ready():
        from ledger.services import posting
        posting.post_transfer(instance)


@receiver(post_delete, sender="cashbook.FundTransfer")
def _transfer_deleted(sender, instance, **kwargs):
    from ledger.models import JournalEntry
    JournalEntry.objects.filter(source_type="transfer", source_id=instance.pk).delete()
