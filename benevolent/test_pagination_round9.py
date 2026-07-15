"""The pagination partial gated on `is_paginated`, a flag only Django's
ListView sets. Eleven modules build a Paginator by hand and pass `page_obj`
without it, so the Prev/Next controls silently rendered nothing on 26 pages —
most visibly the benevolent member list, where a congregation larger than one
page left everyone past member 50 unreachable.

The partial now derives its own visibility from `page_obj`. These tests pin
both the partial's logic and one real end-to-end page (the reported one).
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.template import Context, RequestContext, Template
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentEventType, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()

_PARTIAL = "{% include 'partials/pagination.html' %}"


def _render(page_obj=None, is_paginated=None):
    from django.template import RequestContext
    from django.test import RequestFactory
    ctx = {}
    if page_obj is not None:
        ctx["page_obj"] = page_obj
    if is_paginated is not None:
        ctx["is_paginated"] = is_paginated
    request = RequestFactory().get("/")
    return Template(_PARTIAL).render(RequestContext(request, ctx))


class PartialLogicTests(TestCase):
    def _page(self, total, per_page=10, num=1):
        from django.core.paginator import Paginator
        return Paginator(range(total), per_page).get_page(num)

    def test_controls_show_when_more_than_one_page_and_no_is_paginated_flag(self):
        """The exact hand-rolled-paginator case that used to render nothing."""
        html = _render(page_obj=self._page(35))     # 4 pages, flag absent
        self.assertIn("pagination", html)
        self.assertIn("Next", html)

    def test_controls_hidden_on_a_single_page(self):
        html = _render(page_obj=self._page(5))       # 1 page
        self.assertNotIn("pagination", html)

    def test_no_page_obj_renders_nothing(self):
        self.assertEqual(_render().strip(), "")

    def test_still_works_for_listviews_that_set_the_flag(self):
        html = _render(page_obj=self._page(35), is_paginated=True)
        self.assertIn("Next", html)


class MemberListPaginationTests(TestCase):
    """The reported page: /benevolent/members/ with a full congregation."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t_pg", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

        fund = Department.objects.create(name="PG Fund", slug="pg-fund",
                                         fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent", code="PGB", fund=fund, created_by=self.treasurer)
        BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=900),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"), registration_required=True,
            registration_fee=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("50000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        for i in range(60):     # > one page of 50
            m = Member.objects.create(name=f"PAGE MEMBER {i:03d}")
            reg_svc.register(self.scheme, m,
                             joined_on=TODAY - dt.timedelta(days=100),
                             user=self.treasurer)

    def test_second_page_is_reachable(self):
        url = reverse("benevolent_membership_list")
        r1 = self.client.get(url, {"scheme": self.scheme.pk})
        self.assertEqual(r1.status_code, 200)
        self.assertContains(r1, "pagination")
        self.assertContains(r1, "Next")

        r2 = self.client.get(url, {"scheme": self.scheme.pk, "page": 2})
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "Prev")
        # 60 members, 50 per page => page 2 has the remaining 10
        self.assertEqual(len(r2.context["memberships"]), 10)
