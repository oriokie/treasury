"""The order allocation sources are tried in.

Seven things can claim a bank credit — loan narrations, benevolent scheme
rules, development-group patterns, numbered fund families, exact rules, pattern
rules, and a campaign's member table. Each was added by the module that needed
it, each is configured on its own page, and the order between them was written
into two source files where no treasurer could see it.

That order is what decides the fund. Give a church a reference that two sources
both recognise — "DEVGR7" with an exact rule also written for it — and the money
lands in development or in tithe depending purely on which runs first. Until
this page there was no way to see that, let alone change it.

Some of the order is not preference but accounting: a loan is money the church
owes, and read as development income it overstates income and hides a debt.
Those steps are pinned, the reason is on the page, and the server refuses a
saved order that moves them — a rule that could be shrugged past would be worse
than not offering it.
"""
import datetime as dt

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.models import SiteConfig
from core.roles import ASSISTANT, TREASURER
from core.services import allocation_priority as ap
from departments.models import Department
from giving.models import AllocationRule
from giving.services.allocation import allocate, normalize_reference

URL = "/settings/allocation-priority/"


class RegistryTests(TestCase):
    def test_every_stage_is_described_for_a_treasurer(self):
        for stage in ap.STAGES:
            with self.subTest(stage=stage.key):
                self.assertTrue(stage.label)
                self.assertGreater(len(stage.what.split()), 5)

    def test_a_pin_always_carries_its_reason(self):
        """An unexplained refusal is just an obstacle. Every pin has to say
        what would go wrong in the accounts."""
        for stage in ap.STAGES:
            if not stage.movable:
                with self.subTest(stage=stage.key):
                    self.assertGreater(len(stage.pinned_because.split()), 8)

    def test_the_movable_stages_are_the_ones_allocate_runs(self):
        """The two lists have to agree or the page offers a reorder that
        changes nothing."""
        from giving.services.allocation import STEPS
        self.assertEqual(set(ap.MOVABLE_KEYS), set(STEPS))

    def test_a_blank_configuration_is_the_built_in_order(self):
        self.assertEqual(ap.parse_order(""), ap.default_order())
        self.assertTrue(ap.is_default(""))

    def test_an_unknown_key_is_ignored_rather_than_obeyed(self):
        """Stored config outlives the code that wrote it. A stage renamed or
        removed must not take banking down with it."""
        self.assertEqual(ap.parse_order("not_a_stage"), ap.default_order())

    def test_a_stage_missing_from_the_config_is_restored(self):
        """A stage added after the order was saved still has to run."""
        partial = "exact_rules"
        self.assertEqual(set(ap.parse_order(partial)), set(ap.default_order()))
        self.assertEqual(ap.parse_order(partial)[0], "exact_rules")

    def test_a_duplicate_is_kept_once(self):
        order = ap.parse_order("exact_rules\nexact_rules")
        self.assertEqual(order.count("exact_rules"), 1)


class ValidationTests(TestCase):
    def test_a_legal_reorder_is_accepted(self):
        order = ap.default_order()
        i = order.index("dev_group_numbered")
        order[i], order[i + 1] = order[i + 1], order[i]
        self.assertEqual(ap.validate(order), [])

    def test_moving_a_pinned_stage_is_refused(self):
        order = ap.default_order()
        order.remove("loan_narration")
        order.append("loan_narration")
        problems = ap.validate(order)
        self.assertTrue(problems)
        self.assertIn("cannot be moved", " ".join(problems))

    def test_the_refusal_explains_the_accounting_reason(self):
        order = ap.default_order()
        order.remove("loan_narration")
        order.append("loan_narration")
        self.assertIn("owes", " ".join(ap.validate(order)))

    def test_a_missing_stage_is_refused(self):
        self.assertTrue(ap.validate(["exact_rules"]))


class AllocateHonoursTheOrderTests(TestCase):
    """The point of the whole page: the order decides the fund."""

    def setUp(self):
        self.fund = Department.objects.create(
            name="ApTithe", slug="ap-tithe",
            fund_type=Department.FundType.TRUST,
            category=Department.Category.MINISTRY)
        # Two sources now claim "DEVGR7": the dev-group pattern reads the
        # number, and a treasurer has also written an exact rule for it.
        AllocationRule.objects.create(
            reference=normalize_reference("DEVGR7"), department=self.fund,
            match_type=AllocationRule.MatchType.EXACT,
            source=AllocationRule.Source.SEED)

    def test_by_default_the_development_pattern_wins(self):
        resolver, _ = allocate("DEVGR7")
        self.assertEqual(resolver, "DEV_GROUP_7")

    def test_putting_exact_rules_first_changes_the_fund(self):
        order = ["exact_rules"] + [k for k in ap.MOVABLE_KEYS if k != "exact_rules"]
        resolver, _ = allocate("DEVGR7", order=order)
        self.assertEqual(resolver, self.fund)

    def test_the_saved_configuration_is_what_allocate_uses(self):
        """Not just the explicit argument — the stored order has to take
        effect, or the page is decoration."""
        order = ["exact_rules"] + [k for k in ap.MOVABLE_KEYS if k != "exact_rules"]
        cfg = SiteConfig.get()
        cfg.allocation_priority = "\n".join(order)
        cfg.save()
        resolver, _ = allocate("DEVGR7")
        self.assertEqual(resolver, self.fund)

    def test_an_unreadable_configuration_falls_back_to_working(self):
        """Bad config must degrade to the built-in order, never to an
        exception — this runs inside a bank import."""
        cfg = SiteConfig.get()
        cfg.allocation_priority = "garbage\nmore garbage"
        cfg.save()
        resolver, _ = allocate("DEVGR7")
        self.assertEqual(resolver, "DEV_GROUP_7")

    def test_an_empty_reference_still_goes_to_review(self):
        self.assertEqual(allocate(""), ("UNALLOCATED", "REVIEW"))

    def test_a_reference_nothing_claims_goes_to_review(self):
        self.assertEqual(allocate("zzz nothing here"), ("UNALLOCATED", "REVIEW"))


class TesterTests(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(
            name="ApTithe2", slug="ap-tithe2",
            fund_type=Department.FundType.TRUST,
            category=Department.Category.MINISTRY)
        AllocationRule.objects.create(
            reference=normalize_reference("DEVGR7"), department=self.fund,
            match_type=AllocationRule.MatchType.EXACT,
            source=AllocationRule.Source.SEED)

    def test_it_names_the_winner(self):
        result = ap.explain("DEVGR7")
        self.assertEqual(result["winner"].key, "dev_group_numbered")

    def test_it_shows_the_losers_too(self):
        """The losers ARE the answer. A treasurer looking at money in the wrong
        fund knows where it went; what they cannot see is that something else
        claimed it and the order decided."""
        result = ap.explain("DEVGR7")
        claimed = [r["stage"].key for r in result["rows"] if r["claims"]]
        self.assertIn("exact_rules", claimed)
        self.assertTrue(result["contested"])

    def test_an_uncontested_reference_is_not_reported_as_contested(self):
        result = ap.explain("zzz nothing here")
        self.assertIsNone(result["winner"])
        self.assertEqual(result["contested"], [])

    def test_it_reports_every_stage_whether_it_claims_or_not(self):
        result = ap.explain("DEVGR7")
        self.assertEqual(len(result["rows"]), len(ap.default_order()))

    def test_it_shows_the_reference_as_the_engine_reads_it(self):
        """Half of "why did this go there" is that the narration is normalised
        before any rule sees it."""
        self.assertEqual(ap.explain("Dev Gr 7")["normalised"],
                         normalize_reference("Dev Gr 7"))

    def test_it_changes_nothing(self):
        before = SiteConfig.get().allocation_priority
        rules = AllocationRule.objects.count()
        ap.explain("DEVGR7")
        self.assertEqual(SiteConfig.get().allocation_priority, before)
        self.assertEqual(AllocationRule.objects.count(), rules)

    def test_a_source_that_raises_does_not_break_the_page(self):
        """A tester that can be taken down by one broken rule is no use on the
        day a rule is broken."""
        result = ap.explain(None)
        self.assertIsInstance(result["rows"], list)


class PageTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("ap_tr", password="ap-pass-1",
                                           is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.tr)

    def test_it_opens(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Allocation priority")

    def test_it_shows_every_source_in_order(self):
        body = self.client.get(URL).content.decode()
        positions = [body.index(ap.STAGE_BY_KEY[k].label) for k in ap.default_order()]
        self.assertEqual(positions, sorted(positions))

    def test_a_pin_shows_its_reason_on_the_page_not_in_a_tooltip(self):
        body = self.client.get(URL).content.decode()
        self.assertIn("Cannot be moved.", body)
        self.assertIn("hides a debt", body)

    def test_a_legal_reorder_is_saved(self):
        order = ap.default_order()
        i = order.index("dev_group_numbered")
        order[i], order[i + 1] = order[i + 1], order[i]
        self.client.post(URL, {"order": order})
        self.assertEqual(ap.parse_order(SiteConfig.get().allocation_priority), order)

    def test_moving_a_pinned_source_is_refused_and_says_why(self):
        order = ap.default_order()
        order.remove("campaign_members")
        order.insert(0, "campaign_members")
        r = self.client.post(URL, {"order": order})
        self.assertContains(r, "was not saved")
        self.assertTrue(ap.is_default(SiteConfig.get().allocation_priority))

    def test_a_refused_order_is_shown_back_not_discarded(self):
        """Being bounced to the stored order would lose the work and hide which
        move was refused."""
        order = ap.default_order()
        order.remove("campaign_members")
        order.insert(0, "campaign_members")
        r = self.client.post(URL, {"order": order})
        self.assertEqual([s.key for s in r.context["stages"]][0], "campaign_members")

    def test_saving_the_default_order_stores_nothing(self):
        """So a church that never changed it is not pinned to today's list when
        a source is added later."""
        self.client.post(URL, {"order": ap.default_order()})
        self.assertEqual(SiteConfig.get().allocation_priority, "")

    def test_it_can_be_reset(self):
        cfg = SiteConfig.get()
        cfg.allocation_priority = "exact_rules"
        cfg.save()
        self.client.post(URL, {"action": "reset"})
        self.assertTrue(ap.is_default(SiteConfig.get().allocation_priority))

    def test_the_tester_runs_from_the_page(self):
        r = self.client.post(URL, {"action": "test", "reference": "DEVGR7"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.context["probe"])

    def test_testing_nothing_asks_for_a_reference(self):
        r = self.client.post(URL, {"action": "test", "reference": "  "})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ap.is_default(SiteConfig.get().allocation_priority))


class PermissionTests(TestCase):
    def test_a_clerk_cannot_reorder_allocation(self):
        """Which fund money lands in is a statement-of-accounts question, not a
        data-entry one."""
        clerk = User.objects.create_user("ap_clerk", password="ap-pass-2")
        clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        c = Client()
        c.force_login(clerk)
        order = ap.default_order()
        order[2], order[3] = order[3], order[2]
        r = c.post(URL, {"order": order})
        self.assertIn(r.status_code, (302, 403))
        self.assertTrue(ap.is_default(SiteConfig.get().allocation_priority))

    def test_a_reader_cannot_open_it(self):
        from core.roles import AUDITOR
        reader = User.objects.create_user("ap_read", password="ap-pass-3")
        reader.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        c = Client()
        c.force_login(reader)
        self.assertIn(c.get(URL).status_code, (302, 403))
