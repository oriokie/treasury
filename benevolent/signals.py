"""Keep a case in step with the vouchers that pay it.

The Expense is authoritative: it carries the money, the approval and the ledger
posting. A treasurer working in the ordinary expense screen — approving,
rejecting, or reversing a benevolent payment voucher, quite possibly with no
idea a case sits behind it — must not have to remember to go and update the
case. This signal is what makes that true: the case's payment status is always
derived from its vouchers, never independently maintained.
"""
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver


@receiver(post_save, sender="cashbook.Expense")
def _expense_changed(sender, instance, **kwargs):
    from benevolent.services.cases import sync_case_from_expense
    try:
        sync_case_from_expense(instance)
    except Exception:  # noqa: BLE001 — never break an expense save
        pass


@receiver(pre_delete, sender="cashbook.Expense")
def _expense_deleting(sender, instance, **kwargs):
    """Remember which case (if any) this voucher pays, BEFORE the delete runs.

    By the time post_delete fires, Django's collector has already applied the
    payout's on_delete=SET_NULL, so the link is gone and the case is no longer
    reachable from the expense id. Capturing it here is what lets the case's
    status catch up afterwards.
    """
    try:
        from benevolent.models import BenevolentPayout
        payout = BenevolentPayout.objects.filter(expense_id=instance.pk).first()
        instance._benevolent_case_id = payout.case_id if payout else None
    except Exception:  # noqa: BLE001
        instance._benevolent_case_id = None


@receiver(post_delete, sender="cashbook.Expense")
def _expense_deleted(sender, instance, **kwargs):
    """A deleted voucher leaves its payout row pointing at nothing, which
    correctly makes the payout non-effective — the case just needs to be told so
    its status catches up."""
    case_id = getattr(instance, "_benevolent_case_id", None)
    if not case_id:
        return
    try:
        from benevolent.models import BenevolentCase
        case = BenevolentCase.objects.filter(pk=case_id).first()
        if case:
            case.refresh_status()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# ALWAYS mode: a death recorded ANYWHERE opens a draft case (Round 9, item 1)
#
# The two register services (record_death, record_dependant_death) already open
# a case on the ON_RECORD and ALWAYS settings. These signals catch the remaining
# ALWAYS case: a died_on written by something OTHER than those services — the
# admin, a data import, a future screen. They call the SAME idempotent case
# service, so a death recorded through the register never produces two cases.
# ---------------------------------------------------------------------------

def _auto_open_enabled_for_anywhere():
    from benevolent.models import BenevolentSettings
    return (BenevolentSettings.get().auto_open_case_on_death ==
            BenevolentSettings.DeathCaseMode.ALWAYS)


@receiver(post_save, sender="benevolent.SchemeMembership")
def _membership_death_anywhere(sender, instance, created, **kwargs):
    if created or not instance.died_on:
        return
    if not _auto_open_enabled_for_anywhere():
        return
    try:
        from benevolent.services.cases import open_case_for_death
        open_case_for_death(scheme=instance.scheme, membership=instance,
                            event_date=instance.died_on, user=None)
    except Exception:  # noqa: BLE001 — never break a save
        pass


@receiver(post_save, sender="benevolent.SchemeDependant")
def _dependant_death_anywhere(sender, instance, created, **kwargs):
    if created or not instance.died_on:
        return
    if not _auto_open_enabled_for_anywhere():
        return
    try:
        from benevolent.services.cases import open_case_for_death
        open_case_for_death(scheme=instance.membership.scheme,
                            dependant=instance, event_date=instance.died_on,
                            user=None)
    except Exception:  # noqa: BLE001
        pass
