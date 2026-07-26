"""Every portal page renders for a member whose record exercises its loops.

This suite exists because of a 500 on `/portal/standing/` that three separate
existing suites were structurally unable to see, and it is written to close the
whole class rather than the one line.

**The bug.** `standing.html` rendered `{{ f.message|default:f.name }}` over
`eligibility.Check`, whose fields are `code`/`label`/`passed`/`detail`/
`blocking`. Neither `message` nor `name` exists. A missing variable used as a
FILTER ARGUMENT raises `VariableDoesNotExist` in Django rather than rendering
blank, so the page 500'd — exactly the fault fixed once already in the dues
schedule loop of the same template, left behind in the benefits loop next to it.

**Why nothing caught it.** The failing line only runs for a member who is
*ineligible*: it is inside `{% for f in b.result.blocking_failures %}`. Every
existing fixture had members in good standing, so the loop body never executed.
Of the nine accounts in the seeded demo, eight rendered fine and one — a
reinstated member failing the waiting-period rule — crashed. Same lesson as
#121/#122/#125/#130: a render test whose fixture has no rows tests almost
nothing.

**Why the check has two halves, and why neither alone is enough.**

1. `test_standing_renders_for_a_member_with_a_blocking_failure` asserts a plain
   200 with the default engine. This is the only half that catches the raising
   kind, and it needs a fixture that reaches the loop.

2. `test_no_portal_page_leaves_a_template_variable_unresolved` installs a
   recording `string_if_invalid` sentinel and fails on any unresolved name. This
   is the only half that catches the *silent* kind — `{{ f.message }}` on its
   own renders blank, so a wrong attribute name survives unnoticed until the day
   someone puts it in a filter argument and it becomes a 500.

The sentinel cannot replace the 200 check: with a `%s` sentinel Django's
`FilterExpression.resolve` **returns early** at the invalid variable and never
resolves the filter's arguments, so the very crash this suite is about does not
occur while the sentinel is installed. A sentinel-only suite would have passed
against the live bug. Both halves, or neither works.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.template import engines
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from departments.models import Department
from members.models import Member

from .models import (BenevolentEventType, BenevolentScheme, SchemeMembership,
                     SchemePolicy)
from .services import portal as portal_svc
from .services import registry as reg_svc
from .services import schemes as scheme_svc

PORTAL_PAGES = [
    "portal_home", "portal_contributions", "portal_statement", "portal_standing",
    "portal_household", "portal_cases", "portal_requests", "portal_documents",
    "portal_notifications", "portal_profile",
]


class PortalRenderContractBase(TestCase):
    """A member who is enrolled but *cannot yet claim*.

    The waiting period is set long and the member joined today, so
    `eligibility.evaluate` returns a blocking failure for every event type —
    which is precisely the shape that reaches the loop that used to crash.
    """

    def setUp(self):
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-fund-contract",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.treasurer = User.objects.create_user("tess-contract", password="office-pass-1")
        self.treasurer.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent Scheme", code="BENC", fund=self.fund,
            created_by=self.treasurer, status=BenevolentScheme.Status.ACTIVE)
        self.event_type = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BERC",
            covers_dependants=True)

        policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=dt.date.today() - dt.timedelta(days=500),
            membership_required=True,
            waiting_period_days=365,          # deliberately unmeetable today
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY)
        scheme_svc.publish_policy(policy, user=self.treasurer)

        self.member = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.membership = reg_svc.register(
            self.scheme, self.member, joined_on=dt.date.today())
        if self.membership.status != SchemeMembership.Status.ACTIVE:
            reg_svc.admit(self.membership, notify=False)
            self.membership.refresh_from_db()

        self.account = portal_svc.activate(portal_svc.invite(self.member))
        from accounts.models import UserProfile
        profile = UserProfile.for_user(self.account.user)
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])

        self.client = Client()
        self.client.force_login(self.account.user)

    def assert_fixture_actually_blocks(self):
        """The fixture is only useful if it really produces a blocking failure."""
        from .services import eligibility as elig_svc
        result = elig_svc.evaluate(
            self.scheme, event_type=self.event_type,
            event_date=dt.date.today(), membership=self.membership)
        self.assertTrue(
            result.blocking_failures,
            "Fixture produced no blocking failure, so the loop that used to "
            "crash is never entered and this suite proves nothing.")
        return result


class StandingPageRendersForABlockedMemberTests(PortalRenderContractBase):

    def test_the_fixture_reaches_the_loop_that_used_to_crash(self):
        self.assert_fixture_actually_blocks()

    def test_standing_renders_for_a_member_with_a_blocking_failure(self):
        self.assert_fixture_actually_blocks()
        response = self.client.get(reverse("portal_standing"))
        self.assertEqual(
            response.status_code, 200,
            "The standing page failed for a member who cannot yet claim — the "
            "exact case that returned 500 in production.")

    def test_the_reason_a_benefit_is_blocked_is_actually_shown(self):
        """Guards the fix, not merely the absence of a crash.

        Rendering the wrong field silently (`{{ f.message }}` alone) would still
        return 200 while telling the member nothing, so the assertion is that
        the check's own sentence reaches the page.
        """
        result = self.assert_fixture_actually_blocks()
        body = self.client.get(reverse("portal_standing")).content.decode()
        check = result.blocking_failures[0]
        expected = check.detail or check.label
        self.assertIn(
            expected, body,
            "The blocking reason was computed but not rendered — the member is "
            "told something is in the way without being told what.")

    def test_check_fields_the_template_relies_on_still_exist(self):
        """`Check` is a frozen dataclass the template reads by name.

        Renaming a field here is a silent break of `standing.html`, so the
        contract is pinned rather than left to be discovered in production.
        """
        from .services.eligibility import Check
        for field in ("code", "label", "passed", "detail", "blocking"):
            self.assertIn(
                field, Check.__dataclass_fields__,
                f"standing.html renders Check.{field}; renaming it breaks the page.")


class NoPortalPageLeavesAVariableUnresolvedTests(PortalRenderContractBase):
    """The silent half: any name a portal template asks for must resolve.

    A miss here is not cosmetic. It is how `f.message` survived review — it
    rendered as empty space, and only became a 500 when a sibling name landed in
    a filter argument.
    """

    def _render_with_sentinel(self, url):
        engine = engines["django"].engine
        misses = []

        class Recorder(str):
            def __mod__(self, other):
                misses.append(str(other))
                return ""

        original = engine.string_if_invalid
        engine.string_if_invalid = Recorder("%s")
        try:
            response = self.client.get(url)
        finally:
            engine.string_if_invalid = original
        return response, misses

    def test_no_portal_page_leaves_a_template_variable_unresolved(self):
        failures = []
        for name in PORTAL_PAGES:
            url = reverse(name)
            response, misses = self._render_with_sentinel(url)
            if response.status_code != 200:
                failures.append(f"  {name}: status {response.status_code}")
                continue
            if misses:
                failures.append(f"  {name}: {sorted(set(misses))}")
        self.assertFalse(
            failures,
            "Portal templates asked for names that do not resolve:\n"
            + "\n".join(failures)
            + "\n\nEach is either a typo or a renamed field. Left alone it "
              "renders as blank space until the day it is used as a filter "
              "argument, at which point the page returns 500.")


class EveryPortalPageRendersForABlockedMemberTests(PortalRenderContractBase):

    def test_every_portal_page_renders(self):
        failures = []
        for name in PORTAL_PAGES:
            response = self.client.get(reverse(name))
            if response.status_code != 200:
                failures.append(f"  {name}: {response.status_code}")
        self.assertFalse(
            failures, "Portal pages failed for a blocked member:\n" + "\n".join(failures))
