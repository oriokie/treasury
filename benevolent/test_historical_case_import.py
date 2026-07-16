"""Historical case import (recommendation #73b/#79i) — bringing cases already
decided BEFORE this system existed straight to their known outcome.

Grouped around the claims this makes:

  1. OUTCOME, NOT RE-DECISION   a historical case lands directly at whatever
     the church's records show — no re-run through submit/assess/approve,
     no notification firing for a decision made years ago.
  2. HISTORICAL PAYOUTS         a paid amount is recorded WITHOUT creating a
     live cashbook.Expense — so nothing here can be double-paid through the
     ordinary expense-approval workflow, and the scheme's real fund balance
     (computed purely from live ledger rows) is never touched.
  3. VALIDATION                a working state (SUBMITTED/ASSESSED) is
     refused; a paid amount always needs an approved ceiling.
  4. THE BULK CSV PATH          the same service function, at scale, with
     honest per-row reporting.
  5. TYING IT TO CONTRIBUTIONS  the existing bulk contribution importer can
     find a historically-imported case by its OLD workbook reference, not
     just the newly-issued system number.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentPayout,
                               BenevolentScheme, CaseEvent, SchemeMembership,
                               SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class HistoricalImportFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_hist", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])

        self.fund = Department.objects.create(
            name="Hist Fund", slug="hist-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Hist Scheme", code="HIS", fund=self.fund, created_by=self.treasurer)
        self.event_type = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=2000),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

    def enrol(self, name, days_ago=1500):
        member = Member.objects.create(name=name)
        return reg_svc.register(
            self.scheme, member, joined_on=TODAY - dt.timedelta(days=days_ago),
            user=self.treasurer)


class ImportOutcomeTests(HistoricalImportFixture):
    def test_closed_case_with_paid_amount(self):
        m = self.enrol("Mary Wanjiru")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 3, 4), membership=m,
            status=BenevolentCase.Status.CLOSED,
            approved_amount=Decimal("50000"), paid_amount=Decimal("50000"),
            paid_date=dt.date(2019, 3, 10), user=self.treasurer,
            external_reference="2019/014")

        self.assertEqual(case.status, BenevolentCase.Status.CLOSED)
        self.assertEqual(case.approved_amount, Decimal("50000"))
        self.assertEqual(case.paid_total, Decimal("50000"))
        self.assertEqual(case.external_reference, "2019/014")
        self.assertIsNotNone(case.closed_at)
        # the case number is issued from the EVENT year, not today
        self.assertIn("2019", case.number)

    def test_draft_case_genuinely_undecided(self):
        m = self.enrol("John Kamau")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2021, 1, 10), membership=m,
            status=BenevolentCase.Status.DRAFT, claimed_amount=Decimal("8000"),
            user=self.treasurer)
        self.assertEqual(case.status, BenevolentCase.Status.DRAFT)
        self.assertEqual(case.paid_total, Decimal("0"))
        self.assertIsNone(case.approved_amount)

    def test_partly_paid_case(self):
        m = self.enrol("Peter Otieno")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2020, 7, 19), membership=m,
            status=BenevolentCase.Status.PARTLY_PAID,
            approved_amount=Decimal("12000"), paid_amount=Decimal("5000"),
            user=self.treasurer)
        self.assertEqual(case.paid_total, Decimal("5000"))
        self.assertEqual(case.outstanding, Decimal("7000"))

    def test_rejected_case_needs_no_amounts(self):
        m = self.enrol("Someone Rejected")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2018, 5, 1), membership=m,
            status=BenevolentCase.Status.REJECTED,
            reason="Did not meet the waiting period at the time.",
            user=self.treasurer)
        self.assertEqual(case.status, BenevolentCase.Status.REJECTED)
        self.assertTrue(case.rejection_reason)
        self.assertIsNotNone(case.rejected_by)

    def test_non_member_claim(self):
        """membership may be blank — a community-benevolence case with nobody
        enrolled behind it, exactly as the live create_case() already allows."""
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2020, 1, 1), membership=None,
            beneficiary_name="A Community Family", status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("3000"), paid_amount=Decimal("3000"),
            user=self.treasurer)
        self.assertIsNone(case.membership_id)
        self.assertEqual(case.beneficiary_name, "A Community Family")


class ValidationTests(HistoricalImportFixture):
    def test_working_states_are_refused(self):
        m = self.enrol("X")
        for bad_status in (BenevolentCase.Status.SUBMITTED, BenevolentCase.Status.ASSESSED):
            with self.assertRaises(ValidationError):
                case_svc.import_historical_case(
                    self.scheme, event_type=self.event_type,
                    event_date=dt.date(2020, 1, 1), membership=m,
                    status=bad_status, user=self.treasurer)

    def test_approved_status_needs_approved_amount(self):
        m = self.enrol("Y")
        with self.assertRaises(ValidationError):
            case_svc.import_historical_case(
                self.scheme, event_type=self.event_type,
                event_date=dt.date(2020, 1, 1), membership=m,
                status=BenevolentCase.Status.APPROVED, user=self.treasurer)

    def test_paid_amount_needs_approved_amount(self):
        m = self.enrol("Z")
        with self.assertRaises(ValidationError):
            case_svc.import_historical_case(
                self.scheme, event_type=self.event_type,
                event_date=dt.date(2020, 1, 1), membership=m,
                status=BenevolentCase.Status.CLOSED, paid_amount=Decimal("100"),
                user=self.treasurer)

    def test_paid_cannot_exceed_approved(self):
        m = self.enrol("W")
        with self.assertRaises(ValidationError):
            case_svc.import_historical_case(
                self.scheme, event_type=self.event_type,
                event_date=dt.date(2020, 1, 1), membership=m,
                status=BenevolentCase.Status.CLOSED,
                approved_amount=Decimal("100"), paid_amount=Decimal("200"),
                user=self.treasurer)

    def test_duplicate_external_reference_refused(self):
        m1 = self.enrol("Dup One")
        m2 = self.enrol("Dup Two")
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2020, 1, 1), membership=m1,
            status=BenevolentCase.Status.DRAFT, external_reference="2020/099",
            user=self.treasurer)
        with self.assertRaises(ValidationError):
            case_svc.import_historical_case(
                self.scheme, event_type=self.event_type,
                event_date=dt.date(2020, 2, 1), membership=m2,
                status=BenevolentCase.Status.DRAFT, external_reference="2020/099",
                user=self.treasurer)


class HistoricalPayoutIntegrityTests(HistoricalImportFixture):
    """The core safety property: a historical payout must never be reachable
    through the live expense-approval workflow, and must never touch the
    scheme's real (ledger-derived) fund balance."""

    def test_no_cashbook_expense_is_created(self):
        from cashbook.models import Expense
        before = Expense.objects.count()
        m = self.enrol("No Expense")
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("1000"), paid_amount=Decimal("1000"),
            user=self.treasurer)
        self.assertEqual(Expense.objects.count(), before,
                         "a historical payout must not create a live Expense")

    def test_scheme_fund_balance_is_untouched(self):
        from reports.services import balances
        before = balances.fund_balance(self.fund)
        m = self.enrol("No Balance Effect")
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("7500"), paid_amount=Decimal("7500"),
            user=self.treasurer)
        self.assertEqual(balances.fund_balance(self.fund), before)

    def test_payout_is_marked_historical(self):
        m = self.enrol("Marked")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("400"), paid_amount=Decimal("400"),
            paid_date=dt.date(2019, 2, 2), user=self.treasurer)
        payout = case.payouts.get()
        self.assertTrue(payout.is_historical)
        self.assertIsNone(payout.expense_id)
        self.assertEqual(payout.amount, Decimal("400"))
        self.assertEqual(payout.date, dt.date(2019, 2, 2))
        self.assertTrue(payout.effective)

    def test_status_derives_correctly_from_historical_payout(self):
        """PAID given but paid < approved should self-correct to PARTLY_PAID —
        refresh_status() trusts the numbers over a possibly-wrong label."""
        m = self.enrol("Self Correct")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("1000"), paid_amount=Decimal("600"),
            user=self.treasurer)
        self.assertEqual(case.status, BenevolentCase.Status.PARTLY_PAID)

    def test_closed_partial_payment_is_not_a_live_commitment(self):
        """A case imported as CLOSED with less paid than approved must NOT
        show up as something the scheme still owes — that reporting figure
        (approved_unpaid_total) is specifically for cases still awaiting the
        rest of their payment, which CLOSED explicitly says is not the case."""
        from benevolent.services import reporting as report_svc
        m1 = self.enrol("Closed Partial")
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m1,
            status=BenevolentCase.Status.CLOSED,
            approved_amount=Decimal("1000"), paid_amount=Decimal("400"),
            user=self.treasurer)
        self.assertEqual(report_svc.approved_unpaid_total(self.scheme), Decimal("0"))

    def test_partly_paid_does_count_as_a_live_commitment(self):
        """The contrasting case: PARTLY_PAID genuinely means money is still
        owed, so it SHOULD appear in the outstanding-commitment figure."""
        from benevolent.services import reporting as report_svc
        m2 = self.enrol("Still Owed")
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m2,
            status=BenevolentCase.Status.PARTLY_PAID,
            approved_amount=Decimal("1000"), paid_amount=Decimal("400"),
            user=self.treasurer)
        self.assertEqual(report_svc.approved_unpaid_total(self.scheme), Decimal("600"))


class OngoingCasePolicyTests(HistoricalImportFixture):
    """A bug found while reviewing the module: a historical case left OPEN
    (DRAFT/APPROVED/PARTLY_PAID) whose event predates the scheme's oldest
    policy record could not have a levy raised against it — raise_case_levy()
    falls back to scheme.policy_on(event_date), which is None for a date
    before any policy existed, and hard-fails with "no policy in force".
    Fixed: an ongoing historical case is given a policy to work from."""

    def _old_policy_scheme(self):
        fund = Department.objects.create(
            name="Old Policy Fund", slug="old-policy-fund",
            fund_type=Department.FundType.LOCAL)
        scheme = BenevolentScheme.objects.create(
            name="Old Policy Scheme", code="OPS", fund=fund,
            created_by=self.treasurer)
        et = BenevolentEventType.objects.create(scheme=scheme, name="Ber", code="BER")
        policy = SchemePolicy.objects.create(
            scheme=scheme, effective_from=TODAY - dt.timedelta(days=900),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            contribution_amount=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(scheme, user=self.treasurer)
        return scheme, et

    def test_ongoing_case_predating_any_policy_can_still_raise_a_levy(self):
        scheme, et = self._old_policy_scheme()
        member = Member.objects.create(name="Old Event Member")
        m = reg_svc.register(scheme, member, joined_on=TODAY - dt.timedelta(days=2000),
                             user=self.treasurer)
        old_event = TODAY - dt.timedelta(days=1500)   # well before the policy existed

        case = case_svc.import_historical_case(
            scheme, event_type=et, event_date=old_event, membership=m,
            status=BenevolentCase.Status.PARTLY_PAID,
            approved_amount=Decimal("5000"), paid_amount=Decimal("1000"),
            user=self.treasurer)

        self.assertIsNotNone(case.policy_id,
                             "an ongoing historical case must have a policy to "
                             "work from, or a future levy round hard-fails")
        # must not fail — this is the actual regression
        contrib_svc.raise_case_levy(case)

    def test_closed_case_needs_no_policy(self):
        """A CLOSED case will never have a levy raised against it, so it is
        fine — and more honest — for it to carry no policy reference at all."""
        scheme, et = self._old_policy_scheme()
        member = Member.objects.create(name="Closed Old Event")
        m = reg_svc.register(scheme, member, joined_on=TODAY - dt.timedelta(days=2000),
                             user=self.treasurer)
        old_event = TODAY - dt.timedelta(days=1500)
        case = case_svc.import_historical_case(
            scheme, event_type=et, event_date=old_event, membership=m,
            status=BenevolentCase.Status.CLOSED,
            approved_amount=Decimal("5000"), paid_amount=Decimal("5000"),
            user=self.treasurer)
        self.assertIsNone(case.policy_id)

    def test_snapshots_stay_honestly_blank(self):
        """The fix sets case.policy alone — it must NOT fabricate a frozen
        eligibility snapshot or an assessed_amount, since this case was never
        actually run through the assessment engine."""
        scheme, et = self._old_policy_scheme()
        member = Member.objects.create(name="Honest Blank")
        m = reg_svc.register(scheme, member, joined_on=TODAY - dt.timedelta(days=2000),
                             user=self.treasurer)
        case = case_svc.import_historical_case(
            scheme, event_type=et, event_date=TODAY - dt.timedelta(days=1500),
            membership=m, status=BenevolentCase.Status.APPROVED,
            approved_amount=Decimal("2000"), user=self.treasurer)
        self.assertEqual(case.policy_snapshot, {})
        self.assertEqual(case.eligibility_snapshot, {})
        self.assertIsNone(case.assessed_amount)


class NoNotificationsTests(HistoricalImportFixture):
    def test_no_notification_events_fire(self):
        from benevolent.models import BenevolentNotification
        m = self.enrol("Quiet Import")
        before = BenevolentNotification.objects.count()
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("100"), paid_amount=Decimal("100"),
            user=self.treasurer)
        self.assertEqual(BenevolentNotification.objects.count(), before)


class AuditTrailTests(HistoricalImportFixture):
    def test_imported_event_logged(self):
        m = self.enrol("Logged")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.CLOSED,
            approved_amount=Decimal("500"), paid_amount=Decimal("500"),
            external_reference="2019/077", user=self.treasurer,
            reason="From the 2019 minute book.")
        events = list(case.events.filter(kind=CaseEvent.Kind.IMPORTED))
        self.assertEqual(len(events), 1)
        self.assertIn("2019/077", events[0].summary)
        self.assertIn("minute book", events[0].reason)
        self.assertEqual(events[0].actor, self.treasurer)


class BulkCsvViewTests(HistoricalImportFixture):
    def _csv(self, rows):
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile
        header = ["external_reference", "member_name", "member_phone",
                 "event_type_code", "event_date", "reported_date",
                 "beneficiary_name", "beneficiary_relationship", "status",
                 "claimed_amount", "approved_amount", "paid_amount",
                 "paid_date", "payee_name", "description"]
        lines = [",".join(header)]
        for r in rows:
            lines.append(",".join(str(r.get(h, "")) for h in header))
        content = "\n".join(lines).encode("utf-8")
        return SimpleUploadedFile("cases.csv", content, content_type="text/csv")

    def test_template_download(self):
        self.client.force_login(self.treasurer)
        resp = self.client.get(
            reverse("benevolent_bulk_import_cases", args=[self.scheme.pk]) + "?template=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp["Content-Type"])

    def test_successful_bulk_import(self):
        self.enrol("Mary Wanjiru")
        self.enrol("Peter Otieno")
        f = self._csv([
            {"external_reference": "2019/014", "member_name": "Mary Wanjiru",
             "event_type_code": "BER", "event_date": "2019-03-04",
             "status": "CLOSED", "approved_amount": "50000",
             "paid_amount": "50000", "paid_date": "2019-03-10"},
            {"external_reference": "2020/031", "member_name": "Peter Otieno",
             "event_type_code": "BER", "event_date": "2020-07-19",
             "status": "PAID", "approved_amount": "12000",
             "paid_amount": "10000"},
        ])
        self.client.force_login(self.treasurer)
        resp = self.client.post(
            reverse("benevolent_bulk_import_cases", args=[self.scheme.pk]),
            {"file": f}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BenevolentCase.objects.filter(scheme=self.scheme).count(), 2)
        case1 = BenevolentCase.objects.get(external_reference="2019/014")
        self.assertEqual(case1.status, BenevolentCase.Status.CLOSED)
        case2 = BenevolentCase.objects.get(external_reference="2020/031")
        self.assertEqual(case2.status, BenevolentCase.Status.PARTLY_PAID)  # 10000 < 12000

    def test_bad_row_does_not_sink_the_batch(self):
        self.enrol("Good Row")
        f = self._csv([
            {"external_reference": "OK1", "member_name": "Good Row",
             "event_type_code": "BER", "event_date": "2020-01-01",
             "status": "DRAFT"},
            {"external_reference": "BAD1", "member_name": "Nobody Enrolled",
             "event_type_code": "BER", "event_date": "2020-01-01",
             "status": "DRAFT"},
        ])
        self.client.force_login(self.treasurer)
        resp = self.client.post(
            reverse("benevolent_bulk_import_cases", args=[self.scheme.pk]),
            {"file": f}, follow=True)
        self.assertEqual(BenevolentCase.objects.filter(external_reference="OK1").count(), 1)
        self.assertEqual(BenevolentCase.objects.filter(external_reference="BAD1").count(), 0)

    def test_requires_approve_right(self):
        from core.roles import ASSISTANT
        clerk = User.objects.create_user("clerk_hist", password="x")
        clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.client.force_login(clerk)
        resp = self.client.get(
            reverse("benevolent_bulk_import_cases", args=[self.scheme.pk]))
        self.assertNotEqual(resp.status_code, 200)


class CaseExportIncludesHistoryTests(HistoricalImportFixture):
    """Another gap found while reviewing the module: the case export never
    showed external_reference (defeating cross-checking against the old
    workbook) or how much of a case had actually been PAID — only claimed,
    approved and the funding target."""

    def test_export_includes_external_reference_and_paid_total(self):
        m = self.enrol("Export Row")
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("9000"), paid_amount=Decimal("9000"),
            external_reference="EXPORT-REF-1", user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_case_list"), {"export": "csv"})
        body = resp.content.decode()
        self.assertIn("EXPORT-REF-1", body)
        self.assertIn("9000", body)
        self.assertIn("Old reference", body)


class CaseSearchTests(HistoricalImportFixture):
    """A gap found while reviewing the module: the case-list search matched
    the system number, member name, and beneficiary name — but not
    external_reference, defeating the entire point of recording an old
    workbook reference (being able to look a case up by it)."""

    def test_search_matches_external_reference(self):
        m = self.enrol("Findable")
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.DRAFT, external_reference="2019/MB-042",
            user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_case_list") + "?q=MB-042")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "2019/MB-042")

    def test_case_list_shows_external_reference_under_the_number(self):
        m = self.enrol("Shown")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.DRAFT, external_reference="OLD-REF-7",
            user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_case_list"))
        self.assertContains(resp, "OLD-REF-7")

    def test_case_detail_shows_external_reference(self):
        m = self.enrol("Detail Shown")
        case = case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2019, 1, 1), membership=m,
            status=BenevolentCase.Status.DRAFT, external_reference="OLD-REF-9",
            user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_case_detail", args=[case.pk]))
        self.assertContains(resp, "OLD-REF-9")


class ContributionImportFindsHistoricalCaseTests(HistoricalImportFixture):
    """The tie-in: the EXISTING contribution bulk importer must find a case
    imported here by its OLD workbook reference, not just the system number."""

    def test_case_number_lookup_falls_back_to_external_reference(self):
        m = self.enrol("Levy Payer")
        case_svc.import_historical_case(
            self.scheme, event_type=self.event_type,
            event_date=dt.date(2020, 1, 1), membership=m,
            status=BenevolentCase.Status.DRAFT, external_reference="OLD-REF-42",
            user=self.treasurer)

        from benevolent.views_bulk_import import BulkContributionImportView
        view = BulkContributionImportView()
        amount = view._import_row(self.scheme, {
            "member_name": "Levy Payer", "date": "2020-02-01", "amount": "500",
            "kind": "LEVY", "case_number": "OLD-REF-42", "channel": "CASH",
        }, user=self.treasurer)
        self.assertEqual(amount, Decimal("500"))
        case = BenevolentCase.objects.get(external_reference="OLD-REF-42")
        self.assertEqual(contrib_svc.levy_collected(case), Decimal("500"))
