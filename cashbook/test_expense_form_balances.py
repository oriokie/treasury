"""The expense form's two balance figures, and where the charge field sits.

1. A fund's available balance (shown on the form AND enforced by the overdraw
   guard) includes sub-accounts that collect on its behalf. Where a parent
   keeps spending at the parent level, all the giving lands on the children, so
   judging the parent on its own receipts alone reported it overdrawn while the
   money sat one level down.
2. The Budget item picker reports what is LEFT on each item, not only what was
   budgeted.
3. The transaction charge belongs in "How it was paid", with the method that
   reveals it.
"""
import datetime as dt
import json
import re
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import (Department, collection_descendants,
                                expense_departments, is_directly_chargeable)
from cashbook.models import BudgetLine, Expense
from giving.models import Transaction
from reports.services.balances import fund_balance, spendable_balance


def _give(dept, amount, day=None):
    return Transaction.objects.create(
        date=day or dt.date.today(), channel="BANK", direction="CREDIT",
        amount=Decimal(amount), department=dept, allocation_status="AUTO",
        confirmed=True, core_ref=f"BAL{Transaction.objects.count()+1}")


class CollectionRollupTests(TestCase):
    """The rule itself: which sub-accounts roll up, and what that does to the
    balance."""

    @classmethod
    def setUpTestData(cls):
        # parent that keeps spending at the parent level — its children are
        # collection accounts by that very setting
        cls.lcb = Department.objects.create(
            name="LCB", fund_type="LOCAL", category="MINISTRY",
            children_in_expenses=False)
        cls.loose = Department.objects.create(
            name="Loose Offering", fund_type="LOCAL", category="OFFERING",
            parent=cls.lcb)
        cls.ss = Department.objects.create(
            name="Sabbath School", fund_type="LOCAL", category="OFFERING",
            parent=cls.lcb)
        _give(cls.loose, "6000")
        _give(cls.ss, "4000")

    def test_the_parent_alone_looks_empty(self):
        """The premise. Every shilling is on the children."""
        self.assertEqual(fund_balance(self.lcb), Decimal("0"))

    def test_collection_children_roll_into_the_parent(self):
        self.assertEqual(spendable_balance(self.lcb), Decimal("10000"))

    def test_the_children_are_the_ones_that_cannot_be_charged(self):
        kids = {d.name for d in collection_descendants(self.lcb)}
        self.assertEqual(kids, {"Loose Offering", "Sabbath School"})
        for c in (self.loose, self.ss):
            self.assertFalse(is_directly_chargeable(c))

    def test_a_child_that_can_be_charged_does_not_roll_up(self):
        """It is its own spending unit — counting it here too would make one
        balance spendable from two places."""
        self.lcb.children_in_expenses = True
        self.lcb.save()
        self.assertTrue(is_directly_chargeable(self.loose))
        self.assertEqual(collection_descendants(self.lcb), [])
        self.assertEqual(spendable_balance(self.lcb), Decimal("0"))

    def test_descent_stops_at_a_chargeable_child(self):
        """A grandchild rolls into the nearest fund that can actually spend
        it, not all the way to the top."""
        self.lcb.children_in_expenses = True
        self.lcb.save()
        self.loose.children_in_expenses = False
        self.loose.save()
        grand = Department.objects.create(
            name="Loose · Week 1", fund_type="LOCAL", category="OFFERING",
            parent=self.loose)
        _give(grand, "500")
        self.assertEqual(collection_descendants(self.lcb), [])
        self.assertEqual([d.name for d in collection_descendants(self.loose)],
                         ["Loose · Week 1"])
        self.assertEqual(spendable_balance(self.loose), Decimal("6500"))

    def test_an_internal_transfer_does_not_change_the_family_total(self):
        """Sweeping a collection account into its parent moves money the
        family already had — transfers_out on one side, transfers_in on the
        other, netting to nothing."""
        from cashbook.models import FundTransfer
        who = User.objects.create_user("sweeper", password="x")
        FundTransfer.objects.create(date=dt.date.today(), source=self.loose,
                                    destination=self.lcb, amount=Decimal("1000"),
                                    reason="sweep", recorded_by=who)
        self.assertEqual(spendable_balance(self.lcb), Decimal("10000"))

    def test_a_collection_only_child_rolls_up_even_if_its_parent_allows_children(self):
        self.lcb.children_in_expenses = True
        self.lcb.save()
        self.ss.collection_only = True
        self.ss.save()
        self.assertEqual([d.name for d in collection_descendants(self.lcb)],
                         ["Sabbath School"])
        self.assertEqual(spendable_balance(self.lcb), Decimal("4000"))

    def test_fund_balance_itself_is_unchanged(self):
        """The per-fund figure the general ledger ties to must not move."""
        self.assertEqual(fund_balance(self.lcb), Decimal("0"))
        self.assertEqual(fund_balance(self.loose), Decimal("6000"))


class ExpenseFormBalanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.treasurer = User.objects.create_user("efb_tr", password="x")
        cls.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        cls.lcb = Department.objects.create(
            name="LCB", fund_type="LOCAL", category="MINISTRY",
            children_in_expenses=False)
        cls.loose = Department.objects.create(
            name="Loose Offering", fund_type="LOCAL", category="OFFERING",
            parent=cls.lcb)
        _give(cls.loose, "9000")

    def setUp(self):
        self.client.force_login(self.treasurer)

    def test_the_balance_endpoint_reports_the_rolled_up_figure(self):
        r = self.client.get(reverse("department_balance"), {"id": self.lcb.id})
        d = json.loads(r.content)
        self.assertTrue(d["ok"])
        self.assertEqual(d["balance"], 9000.0)
        self.assertEqual(d["own_balance"], 0.0)
        self.assertEqual([c["name"] for c in d["children"]], ["Loose Offering"])

    def test_the_overdraw_guard_uses_the_same_figure_the_page_showed(self):
        """The defect this closes: the form said the fund had money and the
        save refused it, because the two read different functions."""
        cfg = SiteConfig.get()
        cfg.enforce_fund_balance = True
        cfg.save()
        shown = json.loads(self.client.get(
            reverse("department_balance"), {"id": self.lcb.id}).content)["balance"]
        self.assertEqual(shown, 9000.0)

        r = self.client.post(reverse("expense_create"), {
            "date": dt.date.today().isoformat(), "department": self.lcb.id,
            "description": "Chairs", "amount": "5000", "category": "MATERIALS",
            "method": "CASH", "expenditure_type": "RECURRENT"}, follow=True)
        self.assertTrue(
            Expense.objects.filter(description="Chairs").exists(),
            "the expense was refused even though the page showed 9,000 available")

    def test_it_still_refuses_what_the_family_genuinely_cannot_afford(self):
        cfg = SiteConfig.get()
        cfg.enforce_fund_balance = True
        cfg.save()
        self.client.post(reverse("expense_create"), {
            "date": dt.date.today().isoformat(), "department": self.lcb.id,
            "description": "Too big", "amount": "12000", "category": "MATERIALS",
            "method": "CASH", "expenditure_type": "RECURRENT"})
        self.assertFalse(Expense.objects.filter(description="Too big").exists(),
                         "the roll-up must not switch the overdraw guard off")


class BudgetItemBalanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.treasurer = User.objects.create_user("bib_tr", password="x")
        cls.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        cls.camp = Department.objects.create(name="Camp Meeting", fund_type="LOCAL",
                                             category="DEVELOPMENT")
        cls.year = dt.date.today().year
        cls.catering = BudgetLine.objects.create(
            department=cls.camp, year=cls.year, name="Catering",
            amount=Decimal("30000"))
        cls.beds = BudgetLine.objects.create(
            department=cls.camp, year=cls.year, name="Accommodation",
            amount=Decimal("50000"))
        Expense.objects.create(date=dt.date.today(), department=cls.camp,
                               description="Food", amount=Decimal("28000"),
                               category="REFRESHMENTS", status="PAID",
                               budget_line=cls.catering,
                               recorded_by=cls.treasurer)
        # a PENDING claim has not committed the budget
        Expense.objects.create(date=dt.date.today(), department=cls.camp,
                               description="More food", amount=Decimal("9000"),
                               category="REFRESHMENTS", status="PENDING",
                               budget_line=cls.catering,
                               recorded_by=cls.treasurer)

    def setUp(self):
        self.client.force_login(self.treasurer)

    def _items(self):
        r = self.client.get(reverse("budget_items_json"), {"dept": self.camp.id})
        return {i["name"]: i for i in json.loads(r.content)["items"]}

    def test_each_item_reports_what_is_left(self):
        items = self._items()
        self.assertEqual(items["Catering"]["remaining"], 2000.0)
        self.assertEqual(items["Catering"]["spent"], 28000.0)
        self.assertEqual(items["Accommodation"]["remaining"], 50000.0)

    def test_a_pending_claim_has_not_yet_spent_the_budget(self):
        self.assertEqual(self._items()["Catering"]["spent"], 28000.0)

    def test_an_overspent_item_reports_a_negative_remainder(self):
        Expense.objects.create(date=dt.date.today(), department=self.camp,
                               description="Extra", amount=Decimal("5000"),
                               category="REFRESHMENTS", status="APPROVED",
                               budget_line=self.catering,
                               recorded_by=self.treasurer)
        self.assertEqual(self._items()["Catering"]["remaining"], -3000.0)

    def test_the_budget_page_and_the_picker_agree(self):
        """Both read cashbook.services.budget_items, so they cannot drift."""
        page = self.client.get(reverse("fund_budget", args=[self.camp.id]))
        row = [r for r in page.context["rows"] if r["name"] == "Catering"][0]
        self.assertEqual(float(row["variance"]),
                         self._items()["Catering"]["remaining"])
        self.assertEqual(float(row["actual"]), self._items()["Catering"]["spent"])

    def test_the_form_shows_a_place_for_the_item_balance(self):
        h = self.client.get(reverse("expense_create")).content.decode()
        self.assertIn('id="budgetItemBal"', h)


class ChargeFieldPlacementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.treasurer = User.objects.create_user("cfp_tr", password="x")
        cls.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        Department.objects.create(name="LCB", fund_type="LOCAL", category="MINISTRY")

    def setUp(self):
        self.client.force_login(self.treasurer)

    def test_the_charge_sits_with_the_method_that_reveals_it(self):
        h = self.client.get(reverse("expense_create")).content.decode()
        how_much = h.index("How much, and when")
        how_paid = h.index("How it was paid")
        other = h.index("Other details")
        charge = h.index('name="charge"')
        self.assertTrue(how_paid < charge < other,
                        "the transaction charge is not in 'How it was paid' — it "
                        "is revealed by the method select, which lives there")
        self.assertTrue(charge > how_much)

    def test_the_method_select_and_the_charge_are_in_the_same_group(self):
        """Not just ordered — actually inside one group block, so the reveal
        does not scroll the page."""
        h = self.client.get(reverse("expense_create")).content.decode()
        group = h[h.index("How it was paid"):h.index("Other details")]
        self.assertIn('name="method"', group)
        self.assertIn('name="charge"', group)
