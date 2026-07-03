"""Board report v3: deterministic Camp Meeting Expense Goal selection when
multiple funds are flagged CAMP_EXPENSE, sentence-case fund names throughout
the HTML and Word exports, and Word chart images with AI-analysis captions."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from core.templatetags.treasury_extras import sentence_fund


def _tr():
    u = User.objects.create_user("tr_brv3", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class SentenceFundFilterTests(TestCase):
    def test_all_caps_becomes_sentence_case(self):
        self.assertEqual(sentence_fund("ADVENTIST POSSIBILITY MINISTRY"),
                         "Adventist Possibility Ministry")

    def test_short_acronym_kept_upper(self):
        self.assertEqual(sentence_fund("AMM"), "AMM")
        self.assertEqual(sentence_fund("AMM_CHOIR"), "AMM Choir")

    def test_mixed_case_left_alone(self):
        self.assertEqual(sentence_fund("Camp Meeting Expense"), "Camp Meeting Expense")

    def test_empty_safe(self):
        self.assertEqual(sentence_fund(""), "")
        self.assertIsNone(sentence_fund(None))


class BoardReportSentenceCaseTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="GENERAL EVANGELISM APPEAL",
            fund_type="LOCAL", category="MINISTRY")
        Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("5000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)
        self.c = Client(); self.c.force_login(self.tr)

    def test_html_shows_sentence_case_not_upper(self):
        b = self.c.get("/reports/board/?as_of=2026-06").content.decode()
        self.assertIn("General Evangelism Appeal", b)
        self.assertNotIn("GENERAL EVANGELISM APPEAL", b)

    def test_word_shows_sentence_case_not_upper(self):
        b = self.c.get("/reports/board/export/word/?as_of=2026-06-01").content.decode()
        self.assertIn("General Evangelism Appeal", b)
        self.assertNotIn("GENERAL EVANGELISM APPEAL", b)


class CampExpenseGoalDeterminismTests(TestCase):
    """Regression: an unordered .first() on CAMP_EXPENSE-flagged funds could
    pick one with no year_goal set, making the goal silently vanish from every
    report even though it's really set on a different fund."""
    def test_prefers_fund_with_goal_set(self):
        Department.objects.create(name="StaleCampNoGoalV3", fund_type="LOCAL",
            category="MINISTRY", goal_type="CAMP_EXPENSE", year_goal=None)
        real = Department.objects.create(name="RealCampWithGoalV3", fund_type="LOCAL",
            category="MINISTRY", goal_type="CAMP_EXPENSE", year_goal=Decimal("730000"))
        from reports.views import _camp_goal_records
        rows = _camp_goal_records(2026)
        expense_row = next(r for r in rows if "Expense" in r["name"])
        self.assertEqual(expense_row["goal"], Decimal("730000"))

    def test_board_report_shows_expense_goal(self):
        tr = _tr()
        Department.objects.create(name="CampExpV3", fund_type="LOCAL",
            category="MINISTRY", goal_type="CAMP_EXPENSE", year_goal=Decimal("730000"))
        c = Client(); c.force_login(tr)
        b = c.get("/reports/board/?as_of=2026-06").content.decode()
        self.assertIn("Camp Meeting Expense Goal", b)
        self.assertIn("730,000", b)


class WordChartsAndAiAnalysisTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.exp = Department.objects.create(name="CampExpWordV3", fund_type="LOCAL",
            category="MINISTRY", goal_type="CAMP_EXPENSE", year_goal=Decimal("730000"))
        Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("150000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.exp)
        self.c = Client(); self.c.force_login(self.tr)

    def test_word_export_has_chart_images(self):
        b = self.c.get("/reports/board/export/word/?as_of=2026-06-01").content.decode()
        self.assertIn("data:image/png;base64,", b)

    def test_word_export_has_ai_analysis_captions(self):
        b = self.c.get("/reports/board/export/word/?as_of=2026-06-01").content.decode()
        self.assertIn("AI analysis:", b)

    def test_word_export_has_camp_goal_chart(self):
        b = self.c.get("/reports/board/export/word/?as_of=2026-06-01").content.decode()
        self.assertIn("Camp Meeting Expense Goal", b)

    def test_chart_generation_never_breaks_export_on_missing_data(self):
        # a period with no income/expenditure data at all must still export
        r = self.c.get("/reports/board/export/word/?as_of=2020-01-01")
        self.assertEqual(r.status_code, 200)

    def test_bar_chart_returns_valid_png_data_uri(self):
        from reports.services.chart_image import bar_chart
        uri = bar_chart("Test", [("A", 100, (1, 2, 3)), ("B", 50, (4, 5, 6))])
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        import base64
        raw = base64.b64decode(uri.split(",", 1)[1])
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
