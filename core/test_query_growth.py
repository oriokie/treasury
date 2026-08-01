"""Pages must not cost more queries as the register grows.

A ceiling on a small fixture is a weak guard: an N+1 over a handful of rows
still passes it. What actually distinguishes a per-row query from a page that
is merely busy is whether the count *moves* when rows are added. So these tests
measure the same page twice — once, then again after adding twenty funds — and
require the difference to be nil.

Each case below was a real defect found by the v3.21.0 performance audit:

  * `/ledger/reconciliation/` cost 258 queries and grew by four for every fund.
    A bulk helper, `fund_balances_from_ledger_bulk`, already existed and its own
    docstring names this report as its caller — but only the health check ever
    adopted it, so the two pages did the identical computation at wildly
    different cost. The helper had tests; nothing exercised it *through the
    view*, which is precisely the gap this file closes.
  * `/envelopes/template/` and the envelope ledger called `subgroups_for()` once
    per fund from inside `column_catalog`.
  * Every page carrying a fund dropdown paid one query per sub-account, because
    `Department.__str__` renders "Parent / Child" and so fetched the parent for
    each `<option>`.

The last of those is the reason `test_pages_with_a_fund_selector_stay_flat`
covers several unrelated screens: the fault was not in any of them, it was in
the model, and a guard sitting on one page would not have found it.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from core import roles
from departments.models import Department


class QueryGrowthTestCase(TestCase):
    """Helper: assert a page's query count does not move when funds are added."""

    #: pages to check, as {label: url}
    PAGES = {}
    #: how many funds to add between the two measurements
    ADDED_FUNDS = 20

    def setUp(self):
        self.user = User.objects.create_user(
            "perf_growth", password="office-pass-1", is_superuser=True, is_staff=True)
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        # A register with sub-accounts: the parent/child shape is what made
        # Department.__str__ expensive, so a flat list would not reproduce it.
        for i in range(6):
            parent = Department.objects.create(
                name=f"Base Fund {i}", slug=f"base-fund-{i}",
                fund_type=Department.FundType.LOCAL,
                category=Department.Category.MINISTRY)
            Department.objects.create(
                name=f"Base Sub {i}", slug=f"base-sub-{i}", parent=parent,
                fund_type=Department.FundType.LOCAL,
                category=Department.Category.MINISTRY)
        self.client = Client()
        self.client.force_login(self.user)

    def _queries_for(self, url):
        self.client.get(url)                      # warm templates and caches
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f"{url} -> {response.status_code}")
        return len(ctx.captured_queries)

    def _add_funds(self):
        for i in range(self.ADDED_FUNDS):
            parent = Department.objects.create(
                name=f"Growth Fund {i}", slug=f"growth-fund-{i}",
                fund_type=Department.FundType.LOCAL,
                category=Department.Category.MINISTRY)
            Department.objects.create(
                name=f"Growth Sub {i}", slug=f"growth-sub-{i}", parent=parent,
                fund_type=Department.FundType.LOCAL,
                category=Department.Category.MINISTRY)

    def assert_flat(self, url, tolerance=0):
        before = self._queries_for(url)
        self._add_funds()
        after = self._queries_for(url)
        growth = after - before
        self.assertLessEqual(
            growth, tolerance,
            f"{url} used {before} queries with {Department.objects.count() - 2 * self.ADDED_FUNDS} "
            f"funds and {after} after adding {self.ADDED_FUNDS} more — "
            f"{growth} extra, so the page is querying per fund. Look for a call "
            f"inside a loop over departments, or a queryset that renders "
            f"Department without select_related('parent').")


class ReconciliationDoesNotQueryPerFundTests(QueryGrowthTestCase):

    def test_reconciliation_report_stays_flat(self):
        self.assert_flat("/ledger/reconciliation/")

    def test_health_check_stays_flat(self):
        """The neighbour that was always correct — kept honest alongside it."""
        self.assert_flat("/ledger/health/")

    def test_both_pages_agree_on_every_fund_balance(self):
        """Speed must not have changed the answer.

        The bulk helper is a second implementation of the same accounting, so
        the guard that matters is that it still agrees with the original for
        every fund, including funds with no ledger activity at all.
        """
        from ledger.services import posting
        funds = list(Department.objects.filter(active=True))
        bulk = posting.fund_balances_from_ledger_bulk([d.id for d in funds])
        for fund in funds:
            with self.subTest(fund=fund.name):
                self.assertEqual(
                    bulk.get(fund.id, Decimal(0)),
                    posting.fund_balance_from_ledger(fund),
                    "The bulk ledger balance disagrees with the single-fund "
                    "computation, so the reconciliation report is now lying.")


class EnvelopeScreensDoNotQueryPerFundTests(QueryGrowthTestCase):

    def test_envelope_template_download_stays_flat(self):
        self.assert_flat("/envelopes/template/")

    def test_envelope_entry_stays_flat(self):
        self.assert_flat("/envelopes/")

    def test_column_catalog_cost_does_not_grow_with_the_register(self):
        """The service behind those screens, measured directly.

        Checked here as well as through the views because `column_catalog` is
        called from several places, and a future caller should inherit the
        guarantee rather than need its own test.
        """
        from envelopes.services.posting import column_catalog
        column_catalog()
        with CaptureQueriesContext(connection) as ctx:
            column_catalog()
        before = len(ctx.captured_queries)
        self._add_funds()
        with CaptureQueriesContext(connection) as ctx:
            column_catalog()
        after = len(ctx.captured_queries)
        self.assertEqual(
            before, after,
            f"column_catalog cost {before} queries and now costs {after} after "
            f"{self.ADDED_FUNDS} funds were added — it is querying per fund "
            "again (subgroups_for inside the loop was the original fault).")

    def test_subgroups_are_unchanged_by_the_bulk_path(self):
        """Both paths must shape a fund's subgroups identically."""
        from envelopes.services.posting import column_catalog, subgroups_for
        parent = Department.objects.filter(subgroups__isnull=False).distinct().first()
        self.assertIsNotNone(parent, "Fixture has no fund with sub-accounts.")
        from_catalog = {c["key"]: c for c in column_catalog()}.get(str(parent.id))
        self.assertIsNotNone(from_catalog, "Parent fund missing from the catalogue.")
        self.assertEqual(
            from_catalog["subgroups"], subgroups_for(parent),
            "The catalogue's bulk subgroup lookup disagrees with subgroups_for.")


class FundSelectorPagesStayFlatTests(QueryGrowthTestCase):
    """`Department.__str__` fetches the parent, so any fund dropdown was an N+1.

    These pages have nothing in common except that each renders a form with a
    fund selector, which is the point: the defect lived in the model, and only
    a spread of screens shows that it is fixed at the source.
    """

    def test_allocation_rules_page_stays_flat(self):
        self.assert_flat("/rules/")

    def test_settings_page_stays_flat(self):
        self.assert_flat("/settings/")

    def test_expenses_report_stays_flat(self):
        self.assert_flat("/reports/expenses/")

    def test_department_str_costs_no_query_when_listed(self):
        """The root cause, pinned directly.

        Rendering every department must not cost a query per sub-account. This
        is the assertion that would have caught the fault wherever it appeared,
        rather than one page at a time.
        """
        list(Department.objects.all())              # warm
        with CaptureQueriesContext(connection) as ctx:
            for dept in Department.objects.all():
                str(dept)
        self.assertEqual(
            len(ctx.captured_queries), 1,
            f"Rendering the fund list took {len(ctx.captured_queries)} queries "
            "instead of one. Department.__str__ reads self.parent.name, so the "
            "default manager must select_related('parent') — otherwise every "
            "fund dropdown in the app pays a query per sub-account.")

    def test_department_str_still_shows_the_parent(self):
        """Correctness alongside cost: the label itself must be unchanged."""
        parent = Department.objects.create(
            name="Trust Fund", slug="trust-fund-str",
            fund_type=Department.FundType.TRUST, category=Department.Category.TRUST)
        child = Department.objects.create(
            name="Tithe", slug="tithe-str", parent=parent,
            fund_type=Department.FundType.TRUST, category=Department.Category.TRUST)
        self.assertEqual(str(Department.objects.get(pk=child.pk)), "Trust Fund / Tithe")
        self.assertEqual(str(Department.objects.get(pk=parent.pk)), "Trust Fund")


class RolesUseThePrefetchCacheTests(TestCase):
    """`user_roles` must read groups in a way that honours prefetch_related.

    It used `values_list`, which issues its own query every call and ignores a
    prefetch cache — so a page listing users paid a query per user and no
    caller could fix it from outside. The settings page was doing exactly that.
    """

    def setUp(self):
        group = Group.objects.get_or_create(name=roles.TREASURER)[0]
        for i in range(8):
            user = User.objects.create_user(f"role_perf_{i}", password="x")
            user.groups.add(group)

    def test_listing_users_with_prefetch_costs_one_group_query(self):
        users = list(User.objects.prefetch_related("groups"))
        with CaptureQueriesContext(connection) as ctx:
            for user in users:
                roles.user_roles(user)
        self.assertEqual(
            len(ctx.captured_queries), 0,
            f"Reading roles for {len(users)} prefetched users cost "
            f"{len(ctx.captured_queries)} queries. user_roles must read "
            "user.groups.all() so the prefetch cache is used; values_list "
            "bypasses it and re-queries per user.")

    def test_roles_are_still_correct(self):
        user = User.objects.get(username="role_perf_0")
        self.assertEqual(roles.user_roles(user), {roles.TREASURER})
        self.assertTrue(roles.is_treasurer(user))

    def test_a_user_with_no_groups_has_no_roles(self):
        user = User.objects.create_user("role_perf_none", password="x")
        self.assertEqual(roles.user_roles(user), set())

    def test_a_superuser_keeps_its_implied_roles(self):
        admin = User.objects.create_user("role_perf_admin", password="x", is_superuser=True)
        self.assertEqual(
            roles.user_roles(admin),
            {roles.TREASURER, roles.ASSISTANT, roles.AUDITOR})


class RolesAreMemoisedTests(TestCase):
    """One user, asked repeatedly, is one query.

    The navigation asks whether the login is a treasurer, an assistant, a
    leader, an elder and a portal member, and several views ask again — on a
    portal render that was fourteen identical SELECTs against auth_user_groups
    for a single `request.user`. The memo is per user INSTANCE, which is exactly
    the lifetime of a request.

    A cache of who may do what has to be wrong for no longer than an instant, so
    the invalidation is the part that matters here, not the saving.
    """

    def setUp(self):
        self.group = Group.objects.get_or_create(name=roles.TREASURER)[0]
        self.user = User.objects.create_user("memo_user", password="x")

    def test_repeated_questions_cost_one_query(self):
        roles.user_roles(self.user)                     # first read pays for it
        with CaptureQueriesContext(connection) as ctx:
            for _ in range(10):
                roles.is_treasurer(self.user)
                roles.is_leader(self.user)
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_granting_a_role_is_seen_at_once(self):
        self.assertFalse(roles.is_treasurer(self.user))
        self.user.groups.add(self.group)
        self.assertTrue(roles.is_treasurer(self.user),
                        "a role granted after the memo was warmed went unseen")

    def test_revoking_a_role_is_seen_at_once(self):
        self.user.groups.add(self.group)
        self.assertTrue(roles.is_treasurer(self.user))
        self.user.groups.remove(self.group)
        self.assertFalse(roles.is_treasurer(self.user),
                         "a revoked role was still being honoured from cache")

    def test_a_clear_is_seen_at_once(self):
        self.user.groups.add(self.group)
        self.assertTrue(roles.is_treasurer(self.user))
        self.user.groups.clear()
        self.assertFalse(roles.is_treasurer(self.user))

    def test_two_users_do_not_share_an_answer(self):
        other = User.objects.create_user("memo_other", password="x")
        self.user.groups.add(self.group)
        self.assertTrue(roles.is_treasurer(self.user))
        self.assertFalse(roles.is_treasurer(other))

    def test_the_caller_gets_a_set_of_its_own(self):
        """Callers have always been free to treat the result as theirs; a
        shared set would let one of them edit everybody else's answer."""
        self.user.groups.add(self.group)
        first = roles.user_roles(self.user)
        first.add("NOT A REAL ROLE")
        self.assertEqual(roles.user_roles(self.user), {roles.TREASURER})
