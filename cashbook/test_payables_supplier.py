"""The payables register can link a bill to a supplier as it is entered.

`PayableForm` has always defined `supplier` as a `ModelChoiceField` over the
vendor register. The page did not render it: the add-a-payable form put out only
the free-text `vendor` field. The consequences ran further than a missing input.

  * Every bill entered from this page was saved with `supplier=None`, which is
    exactly what the "N open bills are not linked to a supplier" banner at the
    top of the same page counts. The page was generating the condition it warned
    about, and no amount of clearing the backlog could fix it.
  * `PayableForm.clean()` derives the due date from the supplier's payment terms
    and back-fills `vendor` from the supplier's name. With no supplier ever
    submitted, neither rule could fire, so due dates had to be typed by hand or
    left blank.

So the test is not "an input tag is present". It is that a bill entered through
the page arrives on the supplier's account with the terms applied — the
behaviour the form was written for and the page withheld.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from departments.models import Department
from vendors.models import Vendor

from .models import Expense, Payable


class PayableSupplierSelectorTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tess-pay", password="office-pass-1")
        self.treasurer.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Local Church Budget", slug="lcb-pay",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.supplier = Vendor.objects.create(
            name="Mwangi Hardware", payment_terms=Vendor.Terms.NET30,
            created_by=self.treasurer)
        self.client = Client()
        self.client.force_login(self.treasurer)

    # -- the page offers the choice ------------------------------------------

    def test_the_page_renders_a_supplier_selector(self):
        body = self.client.get(reverse("accruals")).content.decode()
        self.assertIn(
            'name="supplier"', body,
            "The add-a-payable form does not render the supplier field, so every "
            "bill entered here is saved unlinked — which is what the unlinked "
            "warning on this same page counts.")

    def test_the_selector_offers_the_registered_suppliers(self):
        body = self.client.get(reverse("accruals")).content.decode()
        self.assertIn(
            self.supplier.name, body,
            "The supplier register is not offered as choices, so the field is "
            "present but unusable.")

    def test_an_archived_supplier_is_not_offered(self):
        """Selectable must not mean 'everything ever recorded'."""
        Vendor.objects.create(name="Closed Traders Ltd",
                              status=Vendor.Status.ARCHIVED,
                              created_by=self.treasurer)
        body = self.client.get(reverse("accruals")).content.decode()
        self.assertNotIn("Closed Traders Ltd", body,
                         "An archived supplier must not be offered for new bills.")

    # -- and the choice actually does something -------------------------------

    def _post(self, **overrides):
        data = {
            "date": dt.date.today().isoformat(),
            "supplier": self.supplier.pk,
            "vendor": "",
            "description": "Cement for repairs",
            "amount": "1234.00",
            "department": self.fund.pk,
            "category": Expense.Category.MATERIALS,
            "due_date": "",
        }
        data.update(overrides)
        return self.client.post(reverse("payable_create"), data, follow=True)

    def test_a_bill_entered_with_a_supplier_lands_on_that_supplier(self):
        self._post()
        payable = Payable.objects.order_by("-pk").first()
        self.assertIsNotNone(payable, "No payable was created.")
        self.assertEqual(
            payable.supplier_id, self.supplier.pk,
            "The bill was saved without its supplier, so it will not appear on "
            "the supplier's account.")

    def test_a_blank_invoice_name_is_filled_from_the_supplier(self):
        self._post()
        payable = Payable.objects.order_by("-pk").first()
        self.assertEqual(payable.vendor, self.supplier.name)

    def test_the_supplier_terms_set_the_due_date(self):
        """NET30 on a bill dated today falls due in thirty days."""
        today = dt.date.today()
        self._post(date=today.isoformat())
        payable = Payable.objects.order_by("-pk").first()
        self.assertEqual(
            payable.due_date, self.supplier.due_date_for(today),
            "The supplier's payment terms did not set the due date.")

    def test_an_invoice_name_that_differs_is_kept_as_typed(self):
        """The invoice is the record; the supplier is the account it sits on."""
        self._post(vendor="Mwangi Hardware & Sons")
        payable = Payable.objects.order_by("-pk").first()
        self.assertEqual(payable.vendor, "Mwangi Hardware & Sons")
        self.assertEqual(payable.supplier_id, self.supplier.pk)

    def test_a_one_off_bill_with_no_supplier_is_still_allowed(self):
        """Not every purchase is from a registered supplier."""
        self._post(supplier="", vendor="Roadside Welder")
        payable = Payable.objects.order_by("-pk").first()
        self.assertIsNone(payable.supplier_id)
        self.assertEqual(payable.vendor, "Roadside Welder")

    def test_a_bill_with_neither_name_nor_supplier_is_refused(self):
        before = Payable.objects.count()
        self._post(supplier="", vendor="")
        self.assertEqual(Payable.objects.count(), before,
                         "A payable owed to nobody was accepted.")
