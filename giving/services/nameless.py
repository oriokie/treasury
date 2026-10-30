"""Bank credits that arrived without a usable giver name.

STK Push from another site often settles as a Co-op credit whose narration
has a purpose code (``DEVGR21``) where the name should be, and either no
phone or a phone that is not on the register. Those gifts must not invent a
member named after the code — they wait here for a treasurer to link them.

Loan receipts, banking/deposit legs, and other non-income bank credits also
have ``member=None``, but they are not gifts — they are excluded here.
"""
from giving.models import Transaction
from members.services.matching import is_person_name


def nameless_gifts_qs():
    """Active bank credits that still need a person (or an explicit leave-as-is).

    Matching already refuses to create a member from a reference-as-name, so
    an unlinked bank credit is the usual case. Financing and deposit legs are
    filtered out: they share ``member=None`` but are not nameless gifts.
    """
    return (Transaction.objects.active()
            .filter(channel=Transaction.Channel.BANK,
                    direction=Transaction.Direction.CREDIT,
                    member__isnull=True,
                    name_not_required=False,
                    excluded_from_income=False,
                    is_banking=False,
                    loan_receipt__isnull=True)
            .select_related("department", "dev_group", "bank_account")
            .order_by("-date", "-id"))


def needs_name_attention(txn):
    """True when the ledger row still has no real person to show.

    Used by tests and by the live tester; the queue itself uses
    ``nameless_gifts_qs`` (unlinked bank credits that are real income).
    """
    if txn.member_id:
        return False
    if getattr(txn, "name_not_required", False):
        return False
    if txn.channel != Transaction.Channel.BANK:
        return False
    if txn.direction != Transaction.Direction.CREDIT:
        return False
    if txn.is_reversed or txn.is_reversal:
        return False
    if txn.excluded_from_income or txn.is_banking:
        return False
    if getattr(txn, "loan_receipt", None) is not None:
        return False
    return not is_person_name(txn.payer_name)


def link_to_member(txn, member, *, set_payer_name=True):
    """Attach ``member`` to ``txn``. Optionally fill a blank/ref payer_name."""
    if txn is None or member is None:
        return False
    fields = []
    if txn.member_id != member.pk:
        txn.member = member
        fields.append("member")
    if set_payer_name and not is_person_name(txn.payer_name):
        txn.payer_name = member.name
        fields.append("payer_name")
    if member.phone and not (txn.payer_phone or "").strip():
        from members.models import normalize_phone
        txn.payer_phone = normalize_phone(member.phone) or member.phone
        fields.append("payer_phone")
    if txn.name_not_required:
        txn.name_not_required = False
        fields.append("name_not_required")
    if not fields:
        return False
    txn.save(update_fields=fields)
    return True


def leave_as_is(txn):
    """Dismiss from the nameless queue without inventing a member."""
    if txn is None or txn.member_id or txn.name_not_required:
        return False
    txn.name_not_required = True
    txn.save(update_fields=["name_not_required"])
    return True
