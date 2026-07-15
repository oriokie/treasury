"""Member matching: phone first, then unambiguous name/alias, else create."""
from django.db import models, transaction

from members.models import Member, MemberAlias, PossibleDuplicate, normalize_phone, name_key


@transaction.atomic
def match_or_create_member(payer_name, payer_phone):
    """Return (member, outcome) where outcome is one of:
    'matched_phone' | 'matched_name' | 'created'. Never orphans a contribution.
    """
    ph = normalize_phone(payer_phone)
    key = name_key(payer_name)

    # 1) trusted: phone — checks BOTH a member's primary number and any
    # other numbers preserved from a merge (see merge_members below): a
    # payment from someone's second line must still find their existing
    # record, or preserving that number at merge time would be pointless.
    if ph:
        m = Member.objects.filter(
            models.Q(phone=ph) | models.Q(phones__number=ph)).distinct().first()
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


class MemberMergeConflict(Exception):
    """The two records cannot be merged without destroying or duplicating data.

    Raised *before* anything is written, so a refused merge changes nothing.
    `reasons` is a list of human sentences a treasurer can act on.
    """

    def __init__(self, keep, absorb, reasons):
        self.keep = keep
        self.absorb = absorb
        self.reasons = list(reasons)
        super().__init__(
            f"Cannot merge {absorb} into {keep}: " + "; ".join(self.reasons))


# Relations whose rows belong TO the member and are folded rather than blindly
# repointed. Everything else on the graph is repointed automatically — including
# models added in future, which is the point: the previous version hard-coded a
# single relation and silently orphaned the other ten.
_FOLDED = {
    "members.MemberAlias",
    "members.MemberPhone",
    "members.PossibleDuplicate",
}


def _member_relations():
    """Every FK / O2O pointing at Member, excluding the folded ones and
    django-simple-history's shadow tables (which must keep pointing at the
    record as it was — rewriting history would defeat the audit trail)."""
    rels = []
    for rel in Member._meta.related_objects:
        model = rel.related_model
        label = model._meta.label
        if label in _FOLDED:
            continue
        if getattr(model._meta, "proxy", False):
            continue
        if model.__name__.startswith("Historical"):
            continue
        if rel.many_to_many:
            continue
        rels.append(rel)
    return rels


def _unique_groups(rel):
    """The other fields in each uniqueness rule that also covers the member FK.

    `SchemeMembership` has UniqueConstraint(scheme, member) — so its group is
    ('scheme',), meaning: two members in the SAME scheme cannot be collapsed
    into one row. Detecting this from the schema rather than by name means any
    future per-member uniqueness rule is caught for free.
    """
    model, fname = rel.related_model, rel.field.name
    groups = []
    for ut in (getattr(model._meta, "unique_together", ()) or ()):
        if fname in ut:
            groups.append(tuple(f for f in ut if f != fname))
    for con in model._meta.constraints:
        fields = list(getattr(con, "fields", []) or [])
        if fname in fields and getattr(con, "condition", None) is None:
            groups.append(tuple(f for f in fields if f != fname))
    return groups


def merge_conflicts(keep: Member, absorb: Member):
    """Reasons this merge would fail or lose data. Empty list == safe to merge.

    Read-only: safe to call from a template or a pre-flight check.
    """
    reasons = []
    for rel in _member_relations():
        model, fname = rel.related_model, rel.field.name
        groups = _unique_groups(rel)
        if not groups:
            continue
        for group in groups:
            if not group:
                continue
            absorbed = model._default_manager.filter(**{fname: absorb})
            for row in absorbed:
                clash = {f: getattr(row, f + "_id", None) or getattr(row, f)
                         for f in group}
                if model._default_manager.filter(**{fname: keep}, **clash).exists():
                    label = model._meta.verbose_name
                    where = ", ".join(str(getattr(row, f)) for f in group)
                    reasons.append(
                        f"Both records have a {label} for the same {' / '.join(group)}"
                        f" ({where}). Withdraw or transfer one of them first, then merge."
                    )
    return reasons


@transaction.atomic
def merge_members(keep: Member, absorb: Member):
    """Fold `absorb` into `keep`: repoint EVERY relation on the graph, preserve
    both records' names and phone numbers, then delete the absorbed record.

    Raises `MemberMergeConflict` — before writing anything — when repointing
    would violate a per-member uniqueness rule (e.g. both people are registered
    in the same benevolent scheme). Previously that surfaced as a ProtectedError
    500 on the merge page, or worse: a PROTECT relation blocked the delete while
    five SET_NULL relations had already been quietly cut loose.
    """
    from members.models import MemberPhone

    if keep.pk == absorb.pk:
        return keep

    reasons = merge_conflicts(keep, absorb)
    if reasons:
        raise MemberMergeConflict(keep, absorb, reasons)

    # 1) every relation on the graph, not just Transaction
    for rel in _member_relations():
        model, fname = rel.related_model, rel.field.name
        qs = model._default_manager.filter(**{fname: absorb})
        if rel.one_to_one and model._default_manager.filter(**{fname: keep}).exists():
            qs.delete()          # keep's row wins; absorb's would violate the O2O
        else:
            qs.update(**{fname: keep})

    # 2) names: the absorbed spelling, and every alias it had already collected
    known = set(keep.aliases.values_list("name_key", flat=True)) | {keep.name_key}
    for name in [absorb.name] + list(absorb.aliases.values_list("name", flat=True)):
        if name_key(name) and name_key(name) not in known:
            MemberAlias.objects.create(member=keep, name=name)
            known.add(name_key(name))

    # 3) phones: a person who gave from two lines keeps both, one primary
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
    if not keep.phone and seen:
        keep.phone = seen[0]
    keep.save()
    for n in seen:
        MemberPhone.objects.update_or_create(
            member=keep, number=n, defaults={"is_primary": (n == keep.phone)})
    if keep.phone and not keep.phones.filter(is_primary=True).exists():
        keep.phones.filter(number=keep.phone).update(is_primary=True)

    PossibleDuplicate.objects.filter(member__in=[keep, absorb]).update(resolved=True)
    absorb.delete()
    return keep
