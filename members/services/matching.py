"""Member matching: phone first, then unambiguous name/alias, else create."""
from django.db import models, transaction

from members.models import Member, MemberAlias, PossibleDuplicate, normalize_phone, name_key


@transaction.atomic
def match_or_create_member(payer_name, payer_phone):
    """Return (member, outcome) where outcome is one of:
    'matched_phone' | 'matched_name' | 'created'. Never orphans a gift.
    """
    ph = normalize_phone(payer_phone)
    key = name_key(payer_name)

    # 1) trusted: phone
    if ph:
        m = Member.objects.filter(phone=ph).first()
        if m:
            return m, "matched_phone"

    # 2) name / alias, only if unambiguous
    if key:
        qs = (
            Member.objects.filter(models.Q(name_key=key) | models.Q(aliases__name_key=key))
            .distinct()
        )
        if qs.count() == 1:
            m = qs.first()
            if not m.phone and ph:
                m.phone = ph
                m.save()
                return m, "matched_name"
            if not ph or m.phone == ph:
                return m, "matched_name"

    # 3) create, never orphan a gift
    m = Member.objects.create(
        name=(payer_name or "").strip() or "(unknown)",
        phone=ph,
        source=Member.Source.AUTO_BANK,
    )
    if key and Member.objects.filter(name_key=key).exclude(pk=m.pk).exists():
        PossibleDuplicate.objects.get_or_create(member=m)
    return m, "created"


@transaction.atomic
def merge_members(keep: Member, absorb: Member):
    """Repoint transactions/expenses from `absorb` onto `keep`, record the
    absorbed spelling as an alias, preserve BOTH members' phone numbers (so a
    person who paid from two lines keeps both, with one primary for receipting),
    and delete the absorbed record."""
    from giving.models import Transaction
    from members.models import MemberPhone

    Transaction.objects.filter(member=absorb).update(member=keep)
    if absorb.name_key and absorb.name_key != keep.name_key:
        MemberAlias.objects.get_or_create(member=keep, name=absorb.name)

    # gather every distinct number both records knew about
    numbers = []
    for src in (keep, absorb):
        if src.phone:
            numbers.append(src.phone)
        numbers.extend(p.number for p in src.phones.all())
    seen = []
    for n in numbers:
        nn = normalize_phone(n) or n
        if nn and nn not in seen:
            seen.append(nn)
    # keep's existing phone (if any) stays primary; otherwise the first becomes it
    if not keep.phone and seen:
        keep.phone = seen[0]
    keep.save()
    for i, n in enumerate(seen):
        MemberPhone.objects.update_or_create(
            member=keep, number=n,
            defaults={"is_primary": (n == keep.phone)})
    # ensure exactly one primary
    if keep.phone and not keep.phones.filter(is_primary=True).exists():
        keep.phones.filter(number=keep.phone).update(is_primary=True)

    PossibleDuplicate.objects.filter(member__in=[keep, absorb]).update(resolved=True)
    absorb.delete()
    return keep
