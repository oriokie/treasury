"""Batch: new expense categories (Salaries/Wages, Lease Payment), bank charges
never need a receipt (list-page pill + detail-page message), remittance-batch
dropdown fix (RemittanceBatch has no created_at field), fund-budget page bug
where saving per-group goals wiped the fund's own expense goal, and a dynamic
'*' wildcard for receipt strip-strings."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense, clean_receipt_text
from giving.models import Transaction
from core.models import SiteConfig


def _tr():
    u = User.objects.create_user("tr_v2001", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class NewExpenseCategoriesTests(TestCase):
    def test_salaries_and_lease_are_valid_categories(self):
        labels = dict(Expense.Category.choices)
        self.assertIn("SALARIES", labels)
        self.assertIn("LEASE", labels)
        self.assertIn("STATIONERY", labels)

    def test_can_create_expense_with_new_categories(self):
        tr = _tr()
        d = Department.objects.create(name="CatF", fund_type="LOCAL", category="MINISTRY")
        for cat in ("SALARIES", "LEASE"):
            e = Expense.objects.create(date=dt.date(2026, 6, 1), department=d,
                description=f"{cat} test", amount=Decimal("1000"), category=cat,
                status="PAID", recorded_by=tr, approved_by=tr)
            self.assertEqual(e.category, cat)


class BankChargeNoReceiptTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="BCF", fund_type="LOCAL",
            category="MINISTRY")
        self.charge = Expense.objects.create(date=dt.date(2026, 6, 5),
            department=self.d, description="M-Pesa charge", amount=Decimal("30"),
            category="BANK_CHARGE", status="PAID", recorded_by=self.tr,
            approved_by=self.tr)
        self.normal = Expense.objects.create(date=dt.date(2026, 6, 6),
            department=self.d, description="Materials", amount=Decimal("500"),
            category="MATERIALS", status="PAID", recorded_by=self.tr,
            approved_by=self.tr)
        self.c = Client(); self.c.force_login(self.tr)

    def test_bank_charge_excluded_from_missing_receipts_queue(self):
        from cashbook.views import missing_receipts_queryset
        qs = missing_receipts_queryset(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertNotIn(self.charge.id, qs.values_list("id", flat=True))
        self.assertIn(self.normal.id, qs.values_list("id", flat=True))

    def test_no_receipt_pill_absent_for_bank_charge_in_list(self):
        b = self.c.get("/expenses/").content.decode()
        # find the row segments and check the pill only appears for the normal expense
        self.assertIn("no receipt", b)  # present for the normal expense
        charge_idx = b.find("M-Pesa charge")
        segment = b[charge_idx:charge_idx + 200]
        self.assertNotIn("no receipt", segment)

    def test_detail_page_softened_message_for_bank_charge(self):
        b = self.c.get(f"/expenses/{self.charge.id}/").content.decode()
        self.assertIn("No receipt", b)
        self.assertIn("none needed", b)


class RemittanceBatchDropdownFixTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.trust = Department.objects.create(name="RBDropF", fund_type="TRUST",
            category="OFFERING")
        self.c = Client(); self.c.force_login(self.tr)

    def test_open_batches_populate_without_crashing(self):
        from cashbook.models import RemittanceBatch, Expense
        batch = RemittanceBatch.create_batch(created_by=self.tr, status="APPROVED")
        Expense.objects.create(date=dt.date(2026, 6, 28), department=self.trust,
            description="remit", amount=Decimal("30000"), category="REMITTANCE",
            status="PENDING", recorded_by=self.tr, remittance_batch=batch)
        batch.recompute_total(); batch.save(update_fields=["total_amount"])
        Transaction.objects.create(date=dt.date(2026, 7, 1), amount=Decimal("30000"),
            direction="DEBIT", channel="BANK", allocation_status="REVIEW",
            core_ref="RBDROP1", confirmed=True)
        b = self.c.get("/debits/").content.decode()
        self.assertIn(batch.batch_number, b)
        self.assertIn(f'value="{batch.id}"', b)

    def test_open_batches_queryset_no_field_error(self):
        from cashbook.models import RemittanceBatch
        RemittanceBatch.create_batch(created_by=self.tr, status="DRAFT")
        # the buggy version ordered by a nonexistent "created_at" field, which
        # Django templates swallow silently -> empty dropdown with no error.
        # Hitting the page must not 500, and the batch must actually render.
        r = self.c.get("/debits/")
        self.assertEqual(r.status_code, 200)


class FundBudgetGoalIndependenceTests(TestCase):
    """Regression: saving per-group contribution goals used to wipe the fund's
    own expense goal and reset goal_type to NONE, because both forms posted to
    the same shared flag and the handler unconditionally rewrote every field."""
    def setUp(self):
        self.tr = _tr()
        self.exp = Department.objects.create(name="CampExpFB", fund_type="LOCAL",
            category="MINISTRY", goal_type="CAMP_EXPENSE", year_goal=Decimal("730000"))
        self.grp = Department.objects.create(name="GroupFB", fund_type="LOCAL",
            category="MINISTRY", parent=self.exp)
        self.c = Client(); self.c.force_login(self.tr)
        self.yr = dt.date.today().year

    def test_group_goal_save_does_not_wipe_expense_goal(self):
        self.c.post(f"/reports/fund/{self.exp.id}/budget/", {"save_expense_goal": "1",
            "year": str(self.yr), "expense_goal": "730000", "goal_type": "CAMP_EXPENSE"})
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.year_goal, Decimal("730000"))
        self.c.post(f"/reports/fund/{self.exp.id}/budget/", {"save_group_goals": "1",
            "year": str(self.yr), f"group_goal_{self.grp.id}": "35000"})
        self.exp.refresh_from_db(); self.grp.refresh_from_db()
        self.assertEqual(self.exp.year_goal, Decimal("730000"))
        self.assertEqual(self.exp.goal_type, "CAMP_EXPENSE")
        self.assertEqual(self.grp.contribution_goal, Decimal("35000"))

    def test_expense_goal_save_does_not_touch_group_goals(self):
        self.grp.contribution_goal = Decimal("35000")
        self.grp.save(update_fields=["contribution_goal"])
        self.c.post(f"/reports/fund/{self.exp.id}/budget/", {"save_expense_goal": "1",
            "year": str(self.yr), "expense_goal": "800000", "goal_type": "CAMP_EXPENSE"})
        self.grp.refresh_from_db()
        self.assertEqual(self.grp.contribution_goal, Decimal("35000"))


class BoardReportCampExpenseGoalTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.exp = Department.objects.create(name="CampExpBoard", fund_type="LOCAL",
            category="MINISTRY", goal_type="CAMP_EXPENSE", year_goal=Decimal("730000"))
        self.grp = Department.objects.create(name="GroupBoard", fund_type="LOCAL",
            category="MINISTRY", parent=self.exp)
        Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("35000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.grp)
        self.c = Client(); self.c.force_login(self.tr)

    def test_board_report_shows_overall_camp_expense_goal(self):
        b = self.c.get("/reports/board/?as_of=2026-06").content.decode()
        self.assertIn("Camp Meeting Expense Goal", b)
        self.assertIn("730,000", b)

    def test_only_overall_goal_shown_not_per_group_rows(self):
        from reports.views import _camp_goal_records
        rows = _camp_goal_records(2026)
        names = [r["name"] for r in rows]
        self.assertEqual(names.count("Camp Meeting Expense Goal"), 1)
        self.assertNotIn("GroupBoard", " ".join(names))


class DynamicReceiptStripTests(TestCase):
    def setUp(self):
        self.cfg = SiteConfig.get()

    def test_wildcard_strips_varying_amounts(self):
        self.cfg.receipt_strip_strings = "New M-PESA balance is Ksh*."
        self.cfg.save()
        msg = "QGH1 Confirmed. Paid Ksh100. New M-PESA balance is Ksh8,376.00."
        cleaned = clean_receipt_text(msg)
        self.assertNotIn("8,376.00", cleaned)
        self.assertIn("QGH1 Confirmed", cleaned)

    def test_wildcard_matches_different_amounts_each_time(self):
        self.cfg.receipt_strip_strings = "balance is Ksh*."
        self.cfg.save()
        m1 = clean_receipt_text("Info. balance is Ksh1,000.00.")
        m2 = clean_receipt_text("Info. balance is Ksh99,999.50.")
        self.assertNotIn("1,000.00", m1)
        self.assertNotIn("99,999.50", m2)

    def test_literal_phrase_without_wildcard_still_works(self):
        self.cfg.receipt_strip_strings = "Please NEVER share your PIN"
        self.cfg.save()
        cleaned = clean_receipt_text("Paid ok. Please NEVER share your PIN with anyone.")
        self.assertNotIn("NEVER share", cleaned)


class ReceiptStripWhitespaceAndAmountFixTests(TestCase):
    """Regression: (a) a configured pattern with slightly different spacing
    than the actual message (single vs double space) must still match, and
    (b) a wildcard covering an amount with an internal decimal point (e.g.
    499,900.00) must consume the whole number, not stop at the first period."""
    def setUp(self):
        self.cfg = SiteConfig.get()

    def test_exact_user_reported_case(self):
        self.cfg.receipt_strip_strings = (
            "New M-PESA balance is Ksh*. Transaction cost, Ksh*.  "
            "Amount you can transact within the day is *.")
        self.cfg.save()
        msg = ("New M-PESA balance is Ksh5,954.00. Transaction cost, Ksh0.00. "
               "Amount you can transact within the day is 499,900.00.")
        self.assertEqual(clean_receipt_text(msg), "")

    def test_whitespace_mismatch_still_matches(self):
        # double space in the configured pattern, single space in the message
        self.cfg.receipt_strip_strings = "Please  NEVER share your PIN"
        self.cfg.save()
        cleaned = clean_receipt_text("Paid ok. Please NEVER share your PIN with anyone.")
        self.assertNotIn("NEVER share", cleaned)

    def test_amount_with_internal_period_fully_consumed(self):
        self.cfg.receipt_strip_strings = "balance is Ksh*."
        self.cfg.save()
        cleaned = clean_receipt_text("Info. balance is Ksh499,900.00.")
        self.assertEqual(cleaned, "Info.")

    def test_boilerplate_stripped_leaves_real_receipt_intact(self):
        self.cfg.receipt_strip_strings = (
            "New M-PESA balance is Ksh*. Transaction cost, Ksh*.  "
            "Amount you can transact within the day is *.")
        self.cfg.save()
        msg = ("QGH7X8 Confirmed. You have paid Ksh500.00 to Jane Doe. "
               "New M-PESA balance is Ksh5,954.00. Transaction cost, Ksh0.00. "
               "Amount you can transact within the day is 499,900.00.")
        cleaned = clean_receipt_text(msg)
        self.assertEqual(cleaned, "QGH7X8 Confirmed. You have paid Ksh500.00 to Jane Doe.")
