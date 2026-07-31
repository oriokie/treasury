"""Assistant knowledge-base enhancements.

The gap this closes: the app already runs an IntelligenceEngine that produces a
health score, a risk score and prioritised, explained insights — it backs the
Treasurer workspace — but the assistant, which is where a treasurer would
naturally ask "what needs my attention?", had no access to any of it and fell
straight through to "I didn't quite get that".

Three additions, all answered from live data:
  * intelligence digest + recommendations, routed to that same engine;
  * period-on-period comparison ("is giving up or down?");
  * an entity-aware fallback that uses whatever the question DID mention
    (a fund, a member) instead of repeating one static menu at everyone.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from core.services.assistant import _answer_rules as ask
from departments.models import Department
from giving.models import Transaction
from members.models import Member


def _treasurer(username="ai_tr"):
    u = User.objects.create_user(username, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class _Giving(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.tithe = Department.objects.create(name="AiTithe", fund_type="TRUST")
        self.today = dt.date.today()

    def _credit(self, when, amount, dept=None):
        return Transaction.objects.create(
            date=when, service_sabbath=when, amount=Decimal(amount),
            direction="CREDIT", channel="CASH", confirmed=True,
            allocation_status="MANUAL", department=dept or self.tithe)


class IntelligenceDigestTests(_Giving):
    def test_what_needs_my_attention_is_answered_not_a_fallback(self):
        d = ask("what needs my attention")
        self.assertNotIn("_fallback", d)
        self.assertIn("Health", d["text"])

    def test_it_reports_the_health_score_and_band(self):
        d = ask("how are we doing")
        self.assertRegex(d["text"], r"Health \d+/100")
        self.assertIn("/workspace/", d["link"])

    def test_several_phrasings_all_reach_the_engine(self):
        for q in ("any problems", "anything to worry about", "red flags",
                  "are we ok", "financial health", "what's wrong", "alerts"):
            d = ask(q)
            self.assertNotIn("_fallback", d, q)
            self.assertIn("workspace", d.get("link", ""), q)

    def test_recommendations_are_a_separate_answer(self):
        d = ask("what should I do")
        self.assertNotIn("_fallback", d)
        self.assertIn("workspace", d["link"])

    def test_advice_phrasings_reach_the_recommendations(self):
        for q in ("recommendations", "what should i fix", "advice", "priorities"):
            d = ask(q)
            self.assertNotIn("_fallback", d, q)

    def test_a_dismissed_insight_is_not_raised_again(self):
        """Whatever the treasurer closed off in the workspace should stay
        closed when they ask the assistant."""
        from core.intelligence import IntelligenceEngine
        from core.reporting import ReportContext
        from core.models import InsightStatus
        rc = ReportContext.for_period(dt.date(self.today.year, 1, 1), self.today)
        insights = IntelligenceEngine().analyse(rc)
        if not insights:
            self.skipTest("engine produced no insights for this dataset")
        target = sorted(insights, key=lambda i: -i.priority)[0]
        before = ask("what needs my attention this year")
        InsightStatus.objects.create(fingerprint=target.fingerprint,
                                     code=target.code, state="dismissed")
        after = ask("what needs my attention this year")
        titles_before = [r[0] for r in before.get("rows", [])]
        titles_after = [r[0] for r in after.get("rows", [])]
        if target.title in titles_before:
            self.assertNotIn(target.title, titles_after)


class ComparisonTests(_Giving):
    def setUp(self):
        super().setUp()
        first_this = self.today.replace(day=1)
        prev_end = first_this - dt.timedelta(days=1)
        self._credit(first_this, "1000")
        self._credit(prev_end, "400")

    def test_comparison_reports_both_periods_and_the_change(self):
        d = ask("how does this month compare to last month")
        self.assertNotIn("_fallback", d)
        self.assertEqual(len(d["rows"]), 3)
        self.assertEqual(d["rows"][2][0], "Change")

    def test_the_primary_period_is_this_month_not_last(self):
        """parse_period tests "last month" before "this month", so the
        comparison target used to hijack the primary period and the answer
        silently reported the wrong month."""
        d = ask("how does this month compare to last month")
        self.assertIn(self.today.strftime("%B"), d["text"])

    def test_direction_is_stated_in_words(self):
        d = ask("is giving up or down")
        self.assertIn("up", d["note"])

    def test_a_nil_baseline_does_not_divide_by_zero(self):
        from core.services.assistant import _delta_line
        msg = _delta_line(Decimal("100"), Decimal("0"), "Income")
        self.assertIn("nil", msg)
        self.assertNotIn("%", msg)

    def test_no_change_is_said_plainly(self):
        from core.services.assistant import _delta_line
        self.assertIn("unchanged", _delta_line(Decimal("50"), Decimal("50")))

    def test_the_baseline_window_is_the_same_length(self):
        from core.services.assistant import _previous_period
        s, e = dt.date(2026, 6, 1), dt.date(2026, 6, 30)
        ps, pe = _previous_period(s, e)
        self.assertEqual(pe, dt.date(2026, 5, 31))
        self.assertEqual((pe - ps).days, (e - s).days)

    def test_an_unbounded_period_has_no_baseline(self):
        from core.services.assistant import _previous_period
        self.assertEqual(_previous_period(None, None), (None, None))

    def test_a_comparison_can_be_scoped_to_one_fund(self):
        d = ask("compare AiTithe this month to last month")
        self.assertIn("AiTithe", d["text"])


class SmartFallbackTests(_Giving):
    def test_a_recognised_fund_gets_fund_specific_suggestions(self):
        # deliberately NOT a name containing "tithe"/"budget"/etc — those are
        # real keyword rules and would (correctly) answer before the fallback
        fund = Department.objects.create(name="AiOrganRepair", fund_type="LOCAL")
        d = ask("what about the AiOrganRepair situation")
        self.assertTrue(d.get("_fallback"))
        self.assertIn(fund.name, d["text"])
        self.assertTrue(any(fund.name in s for s in d["suggestions"]))
        self.assertIn("link", d)

    def test_a_recognised_member_gets_member_specific_suggestions(self):
        Member.objects.create(name="ZEBEDEE KIPTOO")
        d = ask("tell me about zebedee")
        self.assertTrue(d.get("_fallback"))
        self.assertIn("ZEBEDEE KIPTOO", d["text"])
        self.assertTrue(any("give" in s for s in d["suggestions"]))

    def test_a_short_name_fragment_does_not_match_by_accident(self):
        """Guard against a 3-letter first name matching inside an unrelated
        word and hijacking the answer."""
        Member.objects.create(name="ANN WAMBUI")
        d = ask("flibbertigibbet wibble")
        self.assertNotIn("ANN", d["text"])

    def test_an_unrecognised_question_still_offers_the_menu(self):
        d = ask("explain quantum mechanics")
        self.assertTrue(d.get("_fallback"))
        self.assertTrue(d["suggestions"])


class SuggestionGroupTests(TestCase):
    def test_the_new_capabilities_are_discoverable(self):
        from core.services.assistant import SUGGESTION_GROUPS
        labels = [g["label"] for g in SUGGESTION_GROUPS]
        self.assertIn("Advice & trends", labels)
        items = [i for g in SUGGESTION_GROUPS for i in g["items"]]
        self.assertIn("What needs my attention?", items)

    def test_every_advertised_suggestion_actually_answers(self):
        """A suggestion chip that falls through to "I didn't get that" is
        worse than not offering it. The fund-specific chip is the reason
        suggestion_groups_for_site() exists: hardcoded "Development" is not a
        fund every church has."""
        from core.services.assistant import suggestion_groups_for_site
        _treasurer("ai_tr2")
        Department.objects.create(name="AiOrganRepair", fund_type="LOCAL")
        for g in suggestion_groups_for_site():
            for q in g["items"]:
                d = ask(q)
                self.assertFalse(d.get("_fallback"),
                                 f"suggested question falls back: {q!r}")

    def test_the_example_fund_is_swapped_for_one_that_exists(self):
        from core.services.assistant import suggestion_groups_for_site
        Department.objects.create(name="AiOrganRepair", fund_type="LOCAL")
        items = [i for g in suggestion_groups_for_site() for i in g["items"]]
        self.assertTrue(any("AiOrganRepair" in i for i in items))
        self.assertFalse(any("Development" in i for i in items))

    def test_a_real_development_fund_is_left_alone(self):
        from core.services.assistant import suggestion_groups_for_site
        Department.objects.create(name="Development", fund_type="LOCAL")
        items = [i for g in suggestion_groups_for_site() for i in g["items"]]
        self.assertIn("Balance of Development", items)

    def test_no_funds_at_all_does_not_crash(self):
        from core.services.assistant import suggestion_groups_for_site
        Department.objects.all().delete()
        self.assertTrue(suggestion_groups_for_site())


class SidebarWorkspaceLinkTests(TestCase):
    """The workspace had no sidebar entry for staff at all — it was reachable
    only by typing the URL (the elder nav linked it, the staff nav did not)."""

    def test_a_treasurer_sees_the_link(self):
        self.client.force_login(_treasurer("nav_tr"))
        body = self.client.get("/").content.decode()
        self.assertIn("/workspace/", body)
        self.assertIn("Treasurer workspace", body)

    def test_an_assistant_treasurer_does_not(self):
        from core.roles import ASSISTANT
        u = User.objects.create_user("nav_as", password="x")
        u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.client.force_login(u)
        body = self.client.get("/").content.decode()
        self.assertNotIn("Treasurer workspace", body)

    def test_an_auditor_does_not(self):
        from core.roles import AUDITOR
        u = User.objects.create_user("nav_au", password="x")
        u.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self.client.force_login(u)
        body = self.client.get("/").content.decode()
        self.assertNotIn("Treasurer workspace", body)
