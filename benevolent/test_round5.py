"""Round 5 — reported issues.

The most serious was #4: the register's exception check was flagging entries as
"not in our books" that plainly WERE in the books. The cause was mine — I had
used the reporting date-window as the MATCHING window, so a payment the bank
value-dated 1 July but the treasurer entered on 30 June (when they saw the SMS)
fell outside the index and its statement line was reported as missing. A bank
reference is unique forever; if any transaction carries it, the line is in our
books, whatever date it was recorded under. A reconciliation that cannot survive
a one-day value-date difference is worse than none, because every false positive
teaches a treasurer to stop reading the report.
"""
import csv
import datetime as dt
import io
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import (Department, lcb_fund_ids, receiptable_fund_ids)
from giving.models import DevGroupPattern, Transaction
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentContribution,
                               BenevolentEventType, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from statements.models import BankAccount
from statements.models_register import RegisterException, StatementLine
from statements.services import register as reg_svc_bank

TODAY = dt.date.today()


def _csv_bytes(rows, header):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


class Round5Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_r5", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.account = BankAccount.objects.create(name="Main", account_number="1")
        self.fund = Department.objects.create(
            name="R5 Fund", slug="r5-fund", fund_type=Department.FundType.LOCAL)


# ===========================================================================
# 1. MariaDB-safe constraints
# ===========================================================================

class MariaDbConstraintTests(TestCase):
    """MariaDB does not support conditional unique constraints — it silently
    declines to create them (Django warns W036). So on the production database
    they were not enforced AT ALL, and a duplicate exception could be written.

    The conditions were never needed: all of SQLite, PostgreSQL and MariaDB
    treat NULLs as DISTINCT in a unique index, so an unconditional constraint
    permits any number of line=NULL rows while still enforcing one row per
    (account, kind, line) where line IS set — exactly what the condition
    expressed, and now actually existing on every backend.
    """

    def test_the_constraints_carry_no_condition(self):
        conds = [c for c in RegisterException._meta.constraints]
        self.assertEqual(len(conds), 2)
        for c in conds:
            self.assertIsNone(
                getattr(c, "condition", None),
                f"{c.name} still has a condition — MariaDB will not create it, so it "
                f"will not be enforced in production at all")

    def test_the_system_check_reports_no_W036(self):
        from django.core.checks import run_checks
        w036 = [w for w in run_checks() if getattr(w, "id", "") == "models.W036"]
        self.assertEqual(w036, [], f"W036 warnings remain: {w036}")


# ===========================================================================
# 4. The matching bug — the serious one
# ===========================================================================

class RegisterMatchingTests(Round5Fixture):

    def setUp(self):
        super().setUp()
        reg_svc_bank.import_file(
            self.account,
            path_or_bytes=_csv_bytes(
                [["2026-07-01", "UERZZZ111~x#tithe~254700~C2B~A", "1000", "", "1000"]],
                ["Date", "Narration", "Credit", "Debit", "Balance"]),
            filename="jul.csv", user=self.treasurer)

    def _txn(self, ref, date, amount="1000", account=None):
        return Transaction.objects.create(
            date=date, channel="BANK", direction="CREDIT", amount=Decimal(amount),
            department=self.fund, mpesa_ref=ref, confirmed=True,
            allocation_status="AUTO", reference="tithe",
            bank_account=account)

    def test_a_transaction_recorded_on_a_DIFFERENT_DATE_still_matches(self):
        """The reported bug, exactly. The bank value-dated this 1 July; the
        treasurer entered it 30 June, when they saw the SMS. It IS in the books
        — and used to be flagged as missing, because the matching index was
        built only from transactions inside the reporting window."""
        self._txn("UERZZZ111", dt.date(2026, 6, 30))
        res = reg_svc_bank.recheck(self.account)
        self.assertEqual(res["matched"], 1)
        self.assertFalse(
            RegisterException.objects.filter(
                kind=RegisterException.Kind.MISSING_IN_LEDGER).exists(),
            "a transaction that is plainly in the ledger was reported as missing")

    def test_a_transaction_recorded_much_later_still_matches(self):
        self._txn("UERZZZ111", dt.date(2026, 9, 15))
        res = reg_svc_bank.recheck(self.account)
        self.assertEqual(res["matched"], 1)

    def test_a_genuinely_absent_transaction_is_still_flagged(self):
        """The fix must not blind the check — that would be worse than the bug."""
        res = reg_svc_bank.recheck(self.account)
        self.assertEqual(res["matched"], 0)
        self.assertTrue(RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_LEDGER, ref="UERZZZ111").exists())

    def test_another_accounts_transactions_are_not_flagged_against_this_register(self):
        """With two bank accounts, every transaction of the second must not be
        reported as missing from the first — it was never supposed to be there."""
        other = BankAccount.objects.create(name="Second", account_number="2")
        self._txn("UERZZZ111", dt.date(2026, 7, 1), account=self.account)
        self._txn("OTHERACC1", dt.date(2026, 7, 1), account=other)
        reg_svc_bank.recheck(self.account)
        refs = set(RegisterException.objects.filter(
            account=self.account).values_list("ref", flat=True))
        self.assertNotIn("OTHERACC1", refs)


# ===========================================================================
# 3. Downloading the register
# ===========================================================================

class RegisterDownloadTests(Round5Fixture):

    def test_the_register_downloads_as_csv_and_excel(self):
        reg_svc_bank.import_file(
            self.account,
            path_or_bytes=_csv_bytes(
                [["2026-07-01", "UERAAA111~x#a~254700~C2B~A", "1000", "", "1000"]],
                ["Date", "Narration", "Credit", "Debit", "Balance"]),
            filename="x.csv", user=self.treasurer)
        for fmt, ctype in (("csv", "text/csv"), ("xlsx", "spreadsheet")):
            r = self.client.get(reverse("bank_register"),
                                {"export": fmt, "start": "", "end": ""})
            self.assertEqual(r.status_code, 200, fmt)
            self.assertIn(ctype.split("/")[-1][:8], r["Content-Type"])

    def test_the_download_carries_the_opening_and_closing_balance(self):
        """A register exported without them cannot be checked by anyone."""
        reg_svc_bank.import_file(
            self.account,
            path_or_bytes=_csv_bytes(
                [["2026-07-01", "UERAAA111~x#a~254700~C2B~A", "1000", "", "1000"]],
                ["Date", "Narration", "Credit", "Debit", "Balance"]),
            filename="x.csv", user=self.treasurer)
        r = self.client.get(reverse("bank_register"),
                            {"export": "csv", "start": "", "end": ""})
        body = r.content.decode()
        self.assertIn("OPENING BALANCE", body)
        self.assertIn("CLOSING BALANCE", body)


# ===========================================================================
# 5. Pending receipt = Trust + LCB (including subgroups)
# ===========================================================================

class ReceiptableFundTests(Round5Fixture):

    def setUp(self):
        super().setUp()
        self.trust = Department.objects.create(
            name="R5 Trust", slug="r5-trust", fund_type=Department.FundType.TRUST)
        # deliberately NOT named "LCB" — the point is that CONFIGURING it works,
        # which the old name-only matching ignored entirely
        self.lcb_parent = Department.objects.create(
            name="Budget Main", slug="r5-budget-main",
            fund_type=Department.FundType.LOCAL)
        self.lcb_sub = Department.objects.create(
            name="Budget Youth", slug="r5-budget-youth",
            fund_type=Department.FundType.LOCAL, parent=self.lcb_parent)
        cfg = SiteConfig.get()
        cfg.lcb_departments.set([self.lcb_parent])

    def test_the_configured_lcb_funds_are_honoured_even_with_no_LCB_in_the_name(self):
        ids = lcb_fund_ids()
        self.assertIn(self.lcb_parent.id, ids)

    def test_lcb_SUBGROUPS_are_included(self):
        """LCB money that lands in an LCB subgroup is still LCB money, and a
        treasurer who listed the parent should not have to list every child."""
        self.assertIn(self.lcb_sub.id, lcb_fund_ids())

    def test_receiptable_is_trust_PLUS_the_lcb_family(self):
        r = receiptable_fund_ids()
        self.assertIn(self.trust.id, r)
        self.assertIn(self.lcb_parent.id, r)
        self.assertIn(self.lcb_sub.id, r)
        self.assertNotIn(self.fund.id, r)     # an ordinary local fund is not

    def test_an_unreceipted_LCB_gift_appears_in_pending_receipt(self):
        """It never used to — the list was Trust-only, which is why it was
        called 'Trust pending receipt': a name that described the bug rather
        than the intent."""
        from giving.services.pending_receipt import pending_receipt_rows
        Transaction.objects.create(
            date=TODAY, channel="BANK", direction="CREDIT", amount=Decimal("777"),
            department=self.lcb_sub, confirmed=True, allocation_status="AUTO",
            reference="LCB-777", payer_name="LCB Giver")
        refs = [r[5] for r in pending_receipt_rows()]
        self.assertIn("LCB-777", refs)

    def test_an_ordinary_local_gift_does_NOT_appear(self):
        from giving.services.pending_receipt import pending_receipt_rows
        Transaction.objects.create(
            date=TODAY, channel="BANK", direction="CREDIT", amount=Decimal("50"),
            department=self.fund, confirmed=True, allocation_status="AUTO",
            reference="ORDINARY-50", payer_name="Someone")
        refs = [r[5] for r in pending_receipt_rows()]
        self.assertNotIn("ORDINARY-50", refs)

    def test_the_transaction_list_label_is_renamed(self):
        body = self.client.get(reverse("transaction_list")).content.decode()
        self.assertIn("Pending receipt", body)
        self.assertNotIn("Trust pending receipt", body)

    def test_the_old_export_url_still_works(self):
        """Renaming a URL a treasurer has bookmarked — or that the Telegram bot
        points at — is not a rename, it is a breakage."""
        for key in ("pending-receipt", "trust-pending-receipt"):
            r = self.client.get(reverse("transaction_list"), {"export": key})
            self.assertEqual(r.status_code, 200, key)

    def test_the_pdf_renders(self):
        r = self.client.get(reverse("transaction_list"), {"export": "pending-receipt-pdf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")


# ===========================================================================
# 6. Allocation & categories moved; the duplicate dev-prefix setting retired
# ===========================================================================

class AllocationPageTests(Round5Fixture):

    def test_the_allocation_settings_page_exists(self):
        self.assertEqual(
            self.client.get(reverse("allocation_settings")).status_code, 200)

    def test_the_rules_page_links_to_both_related_pages(self):
        body = self.client.get(reverse("rule_list")).content.decode()
        self.assertIn(reverse("allocation_settings"), body)
        self.assertIn(reverse("dev_patterns"), body)

    def test_dev_patterns_is_gone_from_the_sidebar(self):
        body = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn("Development-group patterns", body)

    def test_the_duplicate_dev_prefix_setting_is_retired(self):
        """It built exactly the regex a DevGroupPattern of kind NUMBERED builds,
        but could not be labelled, ordered, disabled or audited — two places to
        configure one behaviour, neither able to see the other."""
        self.assertFalse(hasattr(SiteConfig.get(), "dev_group_extra_prefixes"))
        from core.forms import SiteConfigForm
        self.assertNotIn("dev_group_extra_prefixes", SiteConfigForm().fields)

    def test_dev_group_patterns_still_do_the_job(self):
        """The capability must survive its consolidation."""
        from giving.services.allocation import allocate
        DevGroupPattern.objects.create(
            label="project + number", pattern=r"(?:project)0*(\d+)",
            kind="NUMBERED", enabled=True, sort_order=500)
        dept, status = allocate("project7")
        self.assertEqual(dept, "DEV_GROUP_7")


# ===========================================================================
# 2. Importing case levies in bulk
# ===========================================================================

class BulkCaseLevyImportTests(Round5Fixture):

    def setUp(self):
        super().setUp()
        scheme_fund = Department.objects.create(
            name="R5 Levy Fund", slug="r5-levy-fund",
            fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="R5 Levy", code="R5L", fund=scheme_fund, created_by=self.treasurer)
        self.event = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=400),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.POOLED,
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.paid = reg_svc.register(
            self.scheme, Member.objects.create(name="R5 Paid", phone="254700500001"),
            joined_on=TODAY - dt.timedelta(days=200), user=self.treasurer)
        self.unpaid = reg_svc.register(
            self.scheme, Member.objects.create(name="R5 Unpaid", phone="254700500002"),
            joined_on=TODAY - dt.timedelta(days=200), user=self.treasurer)
        bereaved = reg_svc.register(
            self.scheme, Member.objects.create(name="R5 Bereaved", phone="254700500003"),
            joined_on=TODAY - dt.timedelta(days=200), user=self.treasurer)
        self.case = case_svc.create_case(
            self.scheme, event_type=self.event, membership=bereaved,
            event_date=TODAY, user=self.treasurer)

    HEADER = ["member_name", "member_phone", "date", "amount", "kind",
              "case_number", "period_label", "channel", "note"]

    def _upload(self, rows):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("levies.csv", _csv_bytes(rows, self.HEADER),
                               content_type="text/csv")
        return self.client.post(
            reverse("benevolent_bulk_import_contributions", args=[self.scheme.pk]),
            {"file": f})

    def test_the_template_offers_a_case_number_column(self):
        r = self.client.get(
            reverse("benevolent_bulk_import_contributions", args=[self.scheme.pk]),
            {"template": "1"})
        self.assertIn(b"case_number", r.content)

    def test_a_levy_imported_with_a_case_number_lands_on_that_cases_roster(self):
        r = self._upload([
            ["R5 Paid", "254700500001", TODAY.isoformat(), "500", "LEVY",
             self.case.number, "", "CASH", ""]])
        self.assertEqual(r.status_code, 200)
        c = BenevolentContribution.objects.get(membership=self.paid)
        self.assertEqual(c.case_id, self.case.pk)
        self.assertEqual(c.kind, BenevolentContribution.Kind.LEVY)
        self.assertEqual(contrib_svc.levy_collected(self.case), Decimal("500"))

    def test_a_whole_roster_can_be_uploaded_paid_and_unpaid_together(self):
        """Edwin's actual ask: import the case roster, whether they contributed
        or not. A blank amount means 'did not contribute' — which is recorded by
        the ABSENCE of a payment, exactly as the levy roster has always derived
        it. Writing a zero-value contribution to say so would put a receipt in
        the ledger for money nobody gave."""
        r = self._upload([
            ["R5 Paid", "254700500001", TODAY.isoformat(), "500", "LEVY",
             self.case.number, "", "CASH", ""],
            ["R5 Unpaid", "254700500002", "", "", "", self.case.number, "", "",
             "did not contribute"],
        ])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["imported"], 1)
        self.assertEqual(r.context["not_contributed"], 1)

        summary = contrib_svc.levy_summary(self.case)
        paid = {x["membership"].pk for x in summary["rows"] if x["paid"] > 0}
        self.assertIn(self.paid.pk, paid)
        self.assertNotIn(self.unpaid.pk, paid)
        # and nothing was written for the one who did not pay
        self.assertFalse(
            BenevolentContribution.objects.filter(membership=self.unpaid).exists())

    def test_an_unknown_case_number_is_reported_not_silently_ignored(self):
        r = self._upload([
            ["R5 Paid", "254700500001", TODAY.isoformat(), "500", "LEVY",
             "BEN-9999-9999", "", "CASH", ""]])
        self.assertContains(r, "No case numbered", status_code=200)
        self.assertFalse(BenevolentContribution.objects.exists())

    def test_a_row_with_no_case_is_still_an_ordinary_contribution(self):
        self._upload([
            ["R5 Paid", "254700500001", TODAY.isoformat(), "100", "", "", "", "CASH", ""]])
        c = BenevolentContribution.objects.get(membership=self.paid)
        self.assertIsNone(c.case_id)
