"""The supplier register."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from cashbook.models import Expense, Payable
from cashbook.services import obligations as obligation_svc
from core.roles import ASSISTANT, TREASURER
from departments.models import Department

from .models import Vendor, VendorBankAccount, VendorContact, name_key
from .services import accounts as account_svc

TODAY = dt.date.today()


class VendorNameKeyTests(TestCase):
    def test_spellings_of_one_business_collapse_to_one_key(self):
        for variant in ["Mwangi Hardware Ltd", "MWANGI HARDWARE",
                        "Mwangi  Hardware  Limited", "Hardware, Mwangi (Ltd)"]:
            self.assertEqual(name_key(variant), name_key("Mwangi Hardware"),
                             f"{variant!r} did not match")

    def test_different_businesses_keep_different_keys(self):
        self.assertNotEqual(name_key("Mwangi Hardware"), name_key("Otieno Hardware"))


class VendorBase(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tess", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Building Fund", slug="building-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.vendor = Vendor.objects.create(
            name="Mwangi Hardware Ltd", payment_terms=Vendor.Terms.NET30,
            created_by=self.treasurer)

    def _bill(self, amount="100000", **kw):
        return Payable.objects.create(
            date=kw.pop("date", TODAY - dt.timedelta(days=40)),
            vendor=self.vendor.name, supplier=self.vendor,
            description=kw.pop("description", "Cement"),
            amount=Decimal(amount), department=self.fund,
            recorded_by=self.treasurer, **kw)


class VendorAccountTests(VendorBase):
    def test_outstanding_is_the_sum_of_unpaid_balances(self):
        self._bill("100000")
        self._bill("50000")
        self.assertEqual(account_svc.outstanding(self.vendor), Decimal("150000"))

    def test_a_part_payment_shows_on_the_supplier_account(self):
        """The supplier account and the balance sheet must agree, because they
        are the same computation — `balance_asof` on the obligation."""
        bill = self._bill("100000")
        obligation_svc.settle(bill, amount=Decimal("40000"), user=self.treasurer)

        self.assertEqual(account_svc.outstanding(self.vendor), Decimal("60000"))
        summary = account_svc.account_summary(self.vendor)
        self.assertEqual(summary["outstanding"], Decimal("60000"))
        self.assertEqual(summary["open_count"], 1)

    def test_the_supplier_account_agrees_with_the_balance_sheet(self):
        from cashbook.services.treasury_position import open_payables_total
        bill = self._bill("100000")
        obligation_svc.settle(bill, amount=Decimal("40000"), user=self.treasurer)
        self.assertEqual(account_svc.outstanding(self.vendor),
                         open_payables_total(),
                         "The supplier profile disagrees with the balance sheet.")

    def test_ageing_buckets_by_how_late_the_bill_is(self):
        self._bill("1000", date=TODAY - dt.timedelta(days=5),
                   due_date=TODAY + dt.timedelta(days=10))     # not yet due
        self._bill("2000", date=TODAY - dt.timedelta(days=50),
                   due_date=TODAY - dt.timedelta(days=20))     # 1-30
        self._bill("4000", date=TODAY - dt.timedelta(days=200),
                   due_date=TODAY - dt.timedelta(days=150))    # 90+

        buckets = account_svc.ageing(self.vendor)
        self.assertEqual(buckets["current"], Decimal("1000"))
        self.assertEqual(buckets["d30"], Decimal("2000"))
        self.assertEqual(buckets["older"], Decimal("4000"))
        self.assertEqual(buckets["total"], Decimal("7000"))

    def test_the_credit_limit_warns_but_does_not_block(self):
        self.vendor.credit_limit = Decimal("50000")
        self.vendor.save()
        self._bill("100000")            # accepted, not refused
        self.assertTrue(account_svc.account_summary(self.vendor)["over_credit_limit"])

    def test_terms_give_a_due_date(self):
        self.assertEqual(self.vendor.due_date_for(dt.date(2026, 6, 1)),
                         dt.date(2026, 7, 1))

    def test_transactions_interleave_bills_and_payments_by_date(self):
        bill = self._bill("100000")
        obligation_svc.settle(bill, amount=Decimal("40000"), user=self.treasurer)
        Expense.objects.filter(payable=bill).update(vendor=self.vendor)

        rows = account_svc.transactions(self.vendor)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], "Payment")     # newest first
        self.assertEqual(rows[1]["kind"], "Invoice")


class VendorLifecycleTests(VendorBase):
    def test_a_near_duplicate_name_is_refused_on_create(self):
        clash = Vendor(name="MWANGI HARDWARE")
        with self.assertRaises(ValidationError):
            clash.full_clean()

    def test_possible_duplicates_are_found(self):
        Vendor.objects.create(name="Mwangi  Hardware")     # bypasses clean()
        self.assertEqual(account_svc.possible_duplicates(self.vendor).count(), 1)

    def test_merging_repoints_everything_and_archives_the_source(self):
        duplicate = Vendor.objects.create(name="Mwangi Hardware")
        bill = Payable.objects.create(
            date=TODAY, vendor="Mwangi Hardware", supplier=duplicate,
            description="Nails", amount=Decimal("5000"),
            department=self.fund, recorded_by=self.treasurer)

        account_svc.merge(duplicate, self.vendor, user=self.treasurer)
        bill.refresh_from_db()
        duplicate.refresh_from_db()

        self.assertEqual(bill.supplier, self.vendor)
        self.assertEqual(duplicate.status, Vendor.Status.ARCHIVED)
        self.assertTrue(Vendor.objects.filter(pk=duplicate.pk).exists(),
                        "The absorbed record must survive so the audit trail resolves.")
        self.assertEqual(account_svc.outstanding(self.vendor), Decimal("5000"))

    def test_archiving_keeps_the_history(self):
        self._bill("1000")
        account_svc.archive(self.vendor, user=self.treasurer, reason="Closed down")
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.status, Vendor.Status.ARCHIVED)
        self.assertEqual(account_svc.outstanding(self.vendor), Decimal("1000"))

    def test_a_supplier_with_bills_cannot_be_deleted(self):
        """PROTECT on the link, because a payable naming a deleted supplier is
        an accounting record pointing at nothing."""
        from django.db.models import ProtectedError
        self._bill("1000")
        with self.assertRaises(ProtectedError):
            self.vendor.delete()

    def test_only_one_primary_contact_and_bank_account(self):
        VendorContact.objects.create(vendor=self.vendor, name="A", is_primary=True)
        second = VendorContact.objects.create(vendor=self.vendor, name="B",
                                              is_primary=True)
        self.assertEqual(
            self.vendor.contacts.filter(is_primary=True).count(), 1)
        self.assertEqual(self.vendor.contacts.get(is_primary=True), second)

    def test_bank_details_start_unverified(self):
        bank = VendorBankAccount.objects.create(
            vendor=self.vendor, bank_name="Equity", account_number="123")
        self.assertFalse(bank.is_verified)


class VendorSearchTests(VendorBase):
    def test_search_finds_by_name_phone_pin_and_contact(self):
        self.vendor.phone = "254711000111"
        self.vendor.tax_pin = "A001234567X"
        self.vendor.save()
        VendorContact.objects.create(vendor=self.vendor, name="Jane Wairimu")

        for term in ["Mwangi", "254711000111", "A001234567X", "Wairimu",
                     "MWANGI HARDWARE LTD"]:
            self.assertIn(self.vendor, account_svc.search(term),
                          f"search failed for {term!r}")

    def test_archived_suppliers_are_hidden_unless_asked_for(self):
        account_svc.archive(self.vendor)
        self.assertNotIn(self.vendor, account_svc.search(""))
        self.assertIn(self.vendor, account_svc.search("", include_archived=True))


class VendorViewTests(VendorBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="tess", password="x")

    def test_the_register_and_profile_render(self):
        self._bill("100000")
        for url in [reverse("vendor_list"),
                    reverse("vendor_detail", args=[self.vendor.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_a_supplier_can_be_created_through_the_form(self):
        self.client.post(reverse("vendor_create"),
                         {"action": "save", "name": "Otieno Printers",
                          "payment_terms": "NET14"})
        self.assertTrue(Vendor.objects.filter(name="Otieno Printers").exists())

    def test_the_lookup_returns_json(self):
        response = self.client.get(reverse("vendor_lookup") + "?q=Mwangi")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["name"], self.vendor.name)

    def test_an_assistant_cannot_archive_a_supplier(self):
        assistant = User.objects.create_user("ass", password="x")
        assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        client = Client()
        client.login(username="ass", password="x")
        client.post(reverse("vendor_archive", args=[self.vendor.pk]),
                    {"reason": "no"})
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.status, Vendor.Status.ACTIVE)


class VendorBackfillTests(TestCase):
    def test_the_backfill_grouped_existing_names(self):
        """The migration ran against the demo data; this pins the rule it used
        rather than the data itself, which differs per install."""
        self.assertEqual(name_key("Mwangi Hardware"), name_key("MWANGI HARDWARE LTD"))


class VendorFormWiringTests(VendorBase):
    """The register only stays accurate if the forms let people choose from it.

    Without this the FK exists and nothing sets it: new bills rely on the
    backfill rule that ran once at migration, and the register goes stale from
    the first invoice entered afterwards.
    """

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="tess", password="x")

    def test_a_payable_can_be_recorded_against_a_supplier(self):
        self.client.post(reverse("payable_create"), {
            "date": TODAY.isoformat(), "supplier": self.vendor.pk,
            "description": "Cement", "amount": "20000",
            "department": self.fund.pk, "category": "MATERIALS"})
        bill = Payable.objects.get(description="Cement")
        self.assertEqual(bill.supplier, self.vendor)

    def test_the_invoice_name_is_filled_from_the_supplier(self):
        """A treasurer who picked the supplier should not have to type the name
        again — that friction is what stops registers being used."""
        self.client.post(reverse("payable_create"), {
            "date": TODAY.isoformat(), "supplier": self.vendor.pk,
            "description": "Steel", "amount": "5000",
            "department": self.fund.pk, "category": "MATERIALS"})
        bill = Payable.objects.get(description="Steel")
        self.assertEqual(bill.vendor, self.vendor.name)

    def test_a_different_invoice_name_is_kept_as_typed(self):
        """The free text records what the document said, so an explicit entry
        must survive being linked to a tidied-up supplier record."""
        self.client.post(reverse("payable_create"), {
            "date": TODAY.isoformat(), "supplier": self.vendor.pk,
            "vendor": "Mwangi Hdwe (Nyamira branch)",
            "description": "Paint", "amount": "3000",
            "department": self.fund.pk, "category": "MATERIALS"})
        bill = Payable.objects.get(description="Paint")
        self.assertEqual(bill.vendor, "Mwangi Hdwe (Nyamira branch)")
        self.assertEqual(bill.supplier, self.vendor)

    def test_a_bill_with_neither_name_nor_supplier_is_refused(self):
        response = self.client.post(reverse("payable_create"), {
            "date": TODAY.isoformat(), "description": "Mystery",
            "amount": "1000", "department": self.fund.pk,
            "category": "MATERIALS"})
        self.assertFalse(Payable.objects.filter(description="Mystery").exists())

    def test_the_due_date_follows_the_suppliers_terms(self):
        self.client.post(reverse("payable_create"), {
            "date": dt.date(2026, 6, 1).isoformat(), "supplier": self.vendor.pk,
            "description": "Timber", "amount": "1000",
            "department": self.fund.pk, "category": "MATERIALS"})
        bill = Payable.objects.get(description="Timber")
        self.assertEqual(bill.due_date, dt.date(2026, 7, 1),
                         "NET30 terms should have set the due date.")

    def test_an_explicit_due_date_beats_the_terms(self):
        self.client.post(reverse("payable_create"), {
            "date": dt.date(2026, 6, 1).isoformat(), "supplier": self.vendor.pk,
            "due_date": dt.date(2026, 6, 10).isoformat(),
            "description": "Nails", "amount": "1000",
            "department": self.fund.pk, "category": "MATERIALS"})
        self.assertEqual(Payable.objects.get(description="Nails").due_date,
                         dt.date(2026, 6, 10))

    def test_settling_a_bill_puts_the_payment_on_the_suppliers_account(self):
        bill = self._bill("10000")
        obligation_svc.settle(bill, amount=Decimal("4000"), user=self.treasurer)
        payment = bill.payments.get()
        self.assertEqual(payment.vendor, self.vendor,
                         "The payment did not land on the supplier's account.")
        # and so it shows in the account history without re-selection
        kinds = [r["kind"] for r in account_svc.transactions(self.vendor)]
        self.assertIn("Payment", kinds)

    def test_archived_suppliers_are_not_offered_in_the_picker(self):
        from cashbook.forms import PayableForm
        account_svc.archive(self.vendor)
        self.assertNotIn(self.vendor,
                         PayableForm().fields["supplier"].queryset)


class VendorBankControlTests(VendorBase):
    """Where a supplier is paid is gated separately from the rest of the record.

    The commonest fraud against a church is a letter announcing that a
    supplier's bank account has changed. Letting anyone with data-entry rights
    make that change — the same right needed to type a phone number — puts the
    control in the wrong place.
    """

    def setUp(self):
        super().setUp()
        self.assistant = User.objects.create_user("ass", password="x")
        self.assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

    def test_an_assistant_cannot_add_payment_details(self):
        client = Client()
        client.login(username="ass", password="x")
        client.post(reverse("vendor_save", args=[self.vendor.pk]), {
            "action": "bank", "bank_name": "Equity", "account_number": "999"})
        self.assertEqual(self.vendor.bank_accounts.count(), 0,
                         "An assistant added supplier bank details.")

    def test_an_assistant_cannot_verify_payment_details(self):
        bank = VendorBankAccount.objects.create(
            vendor=self.vendor, bank_name="Equity", account_number="123")
        client = Client()
        client.login(username="ass", password="x")
        client.post(reverse("vendor_save", args=[self.vendor.pk]),
                    {"action": "verify_bank", "bank_id": bank.pk})
        bank.refresh_from_db()
        self.assertFalse(bank.is_verified)

    def test_a_treasurer_can(self):
        client = Client()
        client.login(username="tess", password="x")
        client.post(reverse("vendor_save", args=[self.vendor.pk]), {
            "action": "bank", "bank_name": "Equity", "account_number": "999"})
        self.assertEqual(self.vendor.bank_accounts.count(), 1)

    def test_an_assistant_can_still_maintain_the_rest_of_the_record(self):
        """The point is a narrow control, not locking the office out."""
        client = Client()
        client.login(username="ass", password="x")
        client.post(reverse("vendor_save", args=[self.vendor.pk]),
                    {"action": "note", "body": "Quoted 5% less than Otieno."})
        self.assertEqual(self.vendor.note_entries.count(), 1)

    def test_bank_detail_changes_are_kept_on_the_record(self):
        """An auditor must be able to see what the account used to be."""
        bank = VendorBankAccount.objects.create(
            vendor=self.vendor, bank_name="Equity", account_number="111")
        bank.account_number = "222"
        bank.save()
        numbers = list(bank.history.values_list("account_number", flat=True))
        self.assertIn("111", numbers)
        self.assertIn("222", numbers)


class VendorAssetLinkTests(VendorBase):
    def test_assets_bought_from_a_supplier_show_on_their_account(self):
        from assets.models import FixedAsset
        FixedAsset.objects.create(
            name="Yamaha keyboard", supplier=self.vendor,
            cost=Decimal("85000"), acquired_on=TODAY - dt.timedelta(days=10))

        kinds = [r["kind"] for r in account_svc.transactions(self.vendor)]
        self.assertIn("Asset", kinds)

    def test_a_supplier_with_assets_cannot_be_deleted(self):
        from django.db.models import ProtectedError
        from assets.models import FixedAsset
        FixedAsset.objects.create(
            name="Yamaha keyboard", supplier=self.vendor,
            cost=Decimal("85000"), acquired_on=TODAY)
        with self.assertRaises(ProtectedError):
            self.vendor.delete()
