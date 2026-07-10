"""Bust the aggregate cache (core.perfcache) whenever financial data changes,
so cached dashboard/executive/controls figures are never stale. No-op overhead
when caching is disabled (the version counter is cheap)."""
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from core.perfcache import bump_data_version, cache_ttl


def _bust(*args, **kwargs):
    if cache_ttl() > 0:
        bump_data_version()


def register():
    from giving.models import Transaction
    from cashbook.models import Expense
    senders = [Transaction, Expense]
    try:
        from cashbook.models import RemittanceBatch, FundTransfer
        senders += [RemittanceBatch, FundTransfer]
    except Exception:
        pass
    for s in senders:
        post_save.connect(_bust, sender=s, dispatch_uid=f"perfbust_save_{s.__name__}")
        post_delete.connect(_bust, sender=s, dispatch_uid=f"perfbust_del_{s.__name__}")
