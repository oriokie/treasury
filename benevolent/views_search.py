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
    a contribution from a donor not (yet) enrolled anywhere."""

    def get(self, request):
        from core.rights import display_phone
        from members.models import Member
        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": []})
        qs = (Member.objects.filter(active=True)
              .filter(Q(name__icontains=q) | Q(phone__icontains=q))
              .order_by("name")[:8])
        results = [{"id": m.id, "name": m.name,
                    "phone": display_phone(request.user, m.phone or ""),
                    "type": m.get_member_type_display() if m.member_type else ""}
                   for m in qs]
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
