"""Member/membership search endpoints for the type-ahead widgets on
benevolent forms (register, contribution, case).

Deliberately NOT a reuse of core.views.MemberSearchView: that endpoint is
gated by DataEntryRequiredMixin (Treasurer/Assistant only), which a Phase
9 role-specific user — a Registration Officer, a Case Officer, a Finance
Officer, none of them necessarily Treasurer or Assistant — would fail. A
search box that 403s for exactly the people Phase 9 built dedicated roles
for is a silent, confusing failure (the dropdown just never shows results),
so this is its own endpoint, gated the way every other read in this module
already is: BenevolentViewMixin.

Two distinct searches, because they answer two different questions:

* member_search  — "who, church-wide, is this?" (registering someone new,
  or crediting a donation from someone not enrolled). Searches
  members.Member directly, same shape as core's own search.
* membership_search — "who, ALREADY ENROLLED IN THIS SCHEME, is this?"
  (raising a case, recording a contribution against an existing
  membership). Scoped to one scheme's ACTIVE memberships — a case cannot be
  raised for someone not enrolled, and offering the whole church roll here
  would suggest otherwise.
"""
from django.db.models import Q
from django.http import JsonResponse
from django.views import View

from core.permissions import BenevolentViewMixin


class MemberSearchView(BenevolentViewMixin, View):
    """Church-wide member typeahead — for registering someone, or crediting
    a contribution from a donor not (yet) enrolled anywhere.

    Searches ALTERNATE phone numbers as well as the primary one. A member who
    pays from a second line is still that member, and `MemberPhone` has always
    existed to record exactly that — but this search only ever looked at
    `Member.phone`, so typing the number a treasurer actually has in front of
    them (from a bank narration, say) would find nobody.

    Each result also carries a WARNING where one applies (Phase-audit item):
    somebody already enrolled in the scheme being registered into, or already
    on record as another member's spouse or dependant, is very often not a new
    principal member at all — they are the same household reached from the
    other side. The registrar sees that before they commit, rather than
    discovering it as an integrity error afterwards or, worse, not at all.
    """

    def get(self, request):
        from core.rights import display_phone
        from members.models import Member
        from .models import SchemeDependant, SchemeMembership

        q = (request.GET.get("q") or "").strip()
        scheme_id = request.GET.get("scheme")   # optional: enables the enrolment warning
        if len(q) < 2:
            return JsonResponse({"results": []})

        qs = (Member.objects.filter(active=True)
              .filter(Q(name__icontains=q) | Q(phone__icontains=q)
                     | Q(phones__number__icontains=q))
              .distinct()
              .prefetch_related("phones")
              .order_by("name")[:8])
        members = list(qs)
        ids = [m.pk for m in members]

        # already enrolled in THIS scheme?
        enrolled = {}
        if scheme_id:
            for sm in (SchemeMembership.objects
                      .filter(scheme_id=scheme_id, member_id__in=ids)
                      .exclude(status=SchemeMembership.Status.WITHDRAWN)):
                enrolled[sm.member_id] = sm

        # already someone else's spouse / dependant?
        dependant_of = {}
        for d in (SchemeDependant.objects
                 .filter(member_id__in=ids, active=True)
                 .select_related("membership__member", "membership__scheme")):
            dependant_of.setdefault(d.member_id, d)

        results = []
        for m in members:
            warning = ""
            existing = enrolled.get(m.pk)
            dep = dependant_of.get(m.pk)
            if existing:
                warning = (f"Already enrolled here as {existing.number} "
                          f"({existing.get_status_display().lower()}).")
            elif dep:
                warning = (f"Already registered as {dep.get_relationship_display().lower()} "
                          f"of {dep.membership.member.name} "
                          f"({dep.membership.scheme.code} {dep.membership.number}).")
            results.append({
                "id": m.id, "name": m.name,
                "phone": display_phone(request.user, m.receipt_phone or m.phone or ""),
                "type": m.get_member_type_display() if m.member_type else "",
                "warning": warning,
                "blocked": bool(existing),
            })
        return JsonResponse({"results": results})


class MembershipSearchView(BenevolentViewMixin, View):
    """Scheme-scoped active-membership typeahead — for raising a case or
    recording a contribution against someone already enrolled."""

    def get(self, request):
        from core.rights import display_phone
        from .models import SchemeMembership
        scheme_id = request.GET.get("scheme")
        q = (request.GET.get("q") or "").strip()
        if not scheme_id or len(q) < 2:
            return JsonResponse({"results": []})
        qs = (SchemeMembership.objects
              .filter(scheme_id=scheme_id, status=SchemeMembership.Status.ACTIVE)
              .filter(Q(member__name__icontains=q) | Q(member__phone__icontains=q)
                     | Q(number__icontains=q))
              .select_related("member").order_by("member__name")[:8])
        results = [{"id": m.id, "name": m.member.name, "number": m.number,
                    "phone": display_phone(request.user, m.member.phone or ""),
                    "standing": m.get_standing_display()}
                   for m in qs]
        return JsonResponse({"results": results})
