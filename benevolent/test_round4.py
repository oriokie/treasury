"""Round 4 — the Bank Statement Register, and the public application form.

Both are deliberately SEPARATE LAYERS:

  * The register records what the BANK says, and never posts, allocates, or
    touches a fund. It exists to be checked against our books, and a register
    that could be quietly "corrected" would be worth nothing.

  * A public application is NOT a membership. Nobody who submits one is
    covered, owes dues, or can claim, until a registration officer approves it
    — at which point they are registered through exactly the same
    `registry.register()` as anyone enrolled at the desk.
"""
import csv
import datetime as dt
import io
import time
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, AUDITOR, TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member

from benevolent.models import (ApplicationDependant, BenevolentApplication,
                               BenevolentEventType, BenevolentScheme,
                               BenevolentSettings, SchemeMembership, SchemePolicy)
from benevolent.services import schemes as scheme_svc
from statements.models import BankAccount
from statements.models_register import (RegisterException, StatementLine,
                                        StatementRegisterImport)
from statements.services import register as reg_svc

TODAY = dt.date.today()


def _csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Narration", "Credit", "Debit", "Balance"])
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


# ===========================================================================
# The parser bug the register uncovered
# ===========================================================================

class IsoDateParsingTests(TestCase):
    """A real, pre-existing bug in the SHARED statement parser, found while
    building the register: `dayfirst=True` scrambles ISO dates.

    dateutil, told day-comes-first, applies that to the `07-01` portion of
    "2026-07-01" even though a leading four-digit year makes the order
    unambiguous — so 1 July came back as 7 January. Any bank exporting ISO
    dates was having its statement silently misdated by up to eleven months,
    in the LEDGER importer as much as in the register.
    """

    def test_an_iso_date_is_read_as_year_month_day(self):
        from statements.services.parser import _to_date
        self.assertEqual(_to_date("2026-07-01"), dt.date(2026, 7, 1))
        self.assertEqual(_to_date("2026-12-31"), dt.date(2026, 12, 31))

    def test_a_day_first_date_is_still_read_day_first(self):
        """The fix must not break the common case: a Kenyan bank writing
        07/01/2026 does mean 7 January."""
        from statements.services.parser import _to_date
        self.assertEqual(_to_date("07/01/2026"), dt.date(2026, 1, 7))

    def test_an_iso_datetime_is_not_scrambled_either(self):
        from statements.services.parser import _to_datetime
        got = _to_datetime("2026-07-01 14:30:00")
        self.assertEqual(got.date(), dt.date(2026, 7, 1))


# ===========================================================================
# The Bank Statement Register
# ===========================================================================

class RegisterFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_r4", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.auditor = User.objects.create_user("a_r4", password="x")
        self.auditor.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        self.account = BankAccount.objects.create(name="Main", account_number="0001")
        self.fund = Department.objects.create(
            name="R4 Fund", slug="r4-fund", fund_type=Department.FundType.LOCAL)
        self.client.force_login(self.treasurer)

    def _import(self, rows, name="stmt.csv"):
        return reg_svc.import_file(self.account, path_or_bytes=_csv(rows),
                                   filename=name, user=self.treasurer)


class RegisterImportTests(RegisterFixture):

    def test_lines_are_recorded_exactly_as_the_bank_sent_them(self):
        imp = self._import([
            ["2026-07-01", "UERAAA111~441211#tithe~254790301470~MPESAC2B~KEVIN O",
             "1000", "", "1000"],
        ])
        self.assertEqual(imp.lines_added, 1)
        ln = StatementLine.objects.get()
        self.assertEqual(ln.date, dt.date(2026, 7, 1))
        self.assertEqual(ln.credit, Decimal("1000"))
        self.assertEqual(ln.bank_balance, Decimal("1000"))
        self.assertEqual(ln.dedup_key, "UERAAA111")

    def test_re_importing_the_same_file_adds_nothing(self):
        rows = [["2026-07-01", "UERAAA111~x#tithe~254700~C2B~A", "1000", "", "1000"]]
        self._import(rows)
        second = self._import(rows)
        self.assertEqual(second.lines_added, 0)
        self.assertEqual(second.duplicates_skipped, 1)
        self.assertEqual(StatementLine.objects.count(), 1)

    def test_an_overlapping_period_adds_only_what_is_new(self):
        """The whole point of 'import from January every month': safe, and it
        picks up exactly the lines the register has not seen."""
        self._import([
            ["2026-07-01", "UERAAA111~x#a~254700~C2B~A", "1000", "", "1000"],
            ["2026-07-02", "UERBBB222~x#b~254701~C2B~B", "500", "", "1500"],
        ])
        second = self._import([
            ["2026-07-02", "UERBBB222~x#b~254701~C2B~B", "500", "", "1500"],
            ["2026-07-03", "UERCCC333~x#c~254702~C2B~C", "2000", "", "3500"],
        ])
        self.assertEqual(second.lines_added, 1)
        self.assertEqual(second.duplicates_skipped, 1)
        self.assertEqual(StatementLine.objects.count(), 3)

    def test_a_line_with_no_bank_reference_still_gets_a_key(self):
        """Bank charges often carry no reference. They are keyed on
        date+amount+narration, and the import SAYS SO — a register that
        silently pretended to be exact would be worse than one that admits
        where it is guessing."""
        imp = self._import([["2026-07-05", "MONTHLY LEDGER FEE", "", "50", "950"]])
        self.assertEqual(imp.lines_added, 1)
        ln = StatementLine.objects.get()
        self.assertTrue(ln.dedup_key.startswith("SYN|"))
        self.assertIn("no bank reference", imp.notes)

    def test_the_register_never_creates_a_transaction(self):
        """The contract that makes this a separate layer: it asserts nothing
        about the money."""
        before = Transaction.objects.count()
        self._import([["2026-07-01", "UERAAA111~x#a~254700~C2B~A", "1000", "", "1000"]])
        self.assertEqual(Transaction.objects.count(), before)


class RunningBalanceTests(RegisterFixture):

    def test_the_running_balance_accumulates_credits_and_debits(self):
        self._import([
            ["2026-07-01", "UERAAA111~x#a~254700~C2B~A", "1000", "", "1000"],
            ["2026-07-02", "UERBBB222~x#b~254701~C2B~B", "500", "", "1500"],
            ["2026-07-03", "BANK CHARGE", "", "50", "1450"],
        ])
        r = reg_svc.running(self.account)
        self.assertEqual(r["closing"], Decimal("1450"))
        self.assertEqual([row["running"] for row in r["rows"]],
                         [Decimal("1000"), Decimal("1500"), Decimal("1450")])

    def test_no_drift_when_our_sum_matches_the_banks_own_balance(self):
        self._import([
            ["2026-07-01", "UERAAA111~x#a~254700~C2B~A", "1000", "", "1000"],
            ["2026-07-02", "UERBBB222~x#b~254701~C2B~B", "500", "", "1500"],
        ])
        self.assertEqual(reg_svc.balance_drift(self.account), [])

    def test_drift_reveals_a_line_the_register_never_received(self):
        """The clearest possible signal that a statement period was missed:
        our sum of what we hold does not reach the balance the bank printed."""
        self._import([
            ["2026-07-01", "UERAAA111~x#a~254700~C2B~A", "1000", "", "1000"],
            # the bank's balance jumps to 5000 — there was a 4000 line we never got
            ["2026-07-03", "UERCCC333~x#c~254702~C2B~C", "500", "", "5000"],
        ])
        drift = reg_svc.balance_drift(self.account)
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["drift"], Decimal("-3500"))


class ExceptionTests(RegisterFixture):

    def setUp(self):
        super().setUp()
        self._import([
            ["2026-07-01", "UERAAA111~x#tithe~254700~C2B~A", "1000", "", "1000"],
            ["2026-07-02", "UERBBB222~x#offering~254701~C2B~B", "500", "", "1500"],
        ])

    def _txn(self, ref, amount, date=dt.date(2026, 7, 1)):
        return Transaction.objects.create(
            date=date, channel="BANK", direction="CREDIT", amount=Decimal(amount),
            department=self.fund, mpesa_ref=ref, confirmed=True,
            allocation_status="AUTO", reference="x")

    def test_a_statement_line_with_no_matching_transaction_is_flagged(self):
        self._txn("UERAAA111", "1000")           # only one of the two recorded
        res = reg_svc.recheck(self.account)
        self.assertEqual(res["matched"], 1)
        exc = RegisterException.objects.get(
            kind=RegisterException.Kind.MISSING_IN_LEDGER)
        self.assertEqual(exc.ref, "UERBBB222")
        self.assertEqual(exc.amount, Decimal("500"))

    def test_a_transaction_the_bank_never_mentioned_is_flagged(self):
        self._txn("UERAAA111", "1000")
        self._txn("UERBBB222", "500", dt.date(2026, 7, 2))
        self._txn("GHOSTREF9", "9999", dt.date(2026, 7, 2))   # bank never sent this
        reg_svc.recheck(self.account)
        exc = RegisterException.objects.get(kind=RegisterException.Kind.MISSING_IN_BANK)
        self.assertEqual(exc.ref, "GHOSTREF9")

    def test_nothing_is_flagged_when_both_sides_agree(self):
        self._txn("UERAAA111", "1000")
        self._txn("UERBBB222", "500", dt.date(2026, 7, 2))
        res = reg_svc.recheck(self.account)
        self.assertEqual(res["matched"], 2)
        self.assertEqual(
            RegisterException.objects.filter(
                status=RegisterException.Status.OPEN).count(), 0)

    def test_the_check_says_NOTHING_about_a_period_the_register_does_not_cover(self):
        """The correction that makes this report usable at all. If the register
        holds only July, it knows nothing about June — so comparing our June
        ledger against it would flag every June transaction as 'missing from
        the bank', when the bank has simply not been asked. That is an absence
        of evidence, not a discrepancy, and reporting it as one would bury the
        real exceptions under hundreds of false ones."""
        self._txn("JUNEREF11", "700", dt.date(2026, 6, 15))   # outside the register
        self._txn("UERAAA111", "1000")
        self._txn("UERBBB222", "500", dt.date(2026, 7, 2))
        reg_svc.recheck(self.account)
        refs = set(RegisterException.objects.values_list("ref", flat=True))
        self.assertNotIn("JUNEREF11", refs)

    def test_an_exception_explained_by_a_later_import_closes_itself(self):
        reg_svc.recheck(self.account)      # both lines unmatched
        self.assertEqual(RegisterException.objects.filter(
            status=RegisterException.Status.OPEN).count(), 2)
        self._txn("UERAAA111", "1000")     # now recorded
        res = reg_svc.recheck(self.account)
        self.assertEqual(res["auto_closed"], 1)
        self.assertEqual(RegisterException.objects.filter(
            status=RegisterException.Status.OPEN).count(), 1)

    def test_a_person_resolved_exception_is_never_re_opened(self):
        """A discrepancy report that re-raises the same explained item every
        time it runs teaches a treasurer to ignore it — which is the opposite
        of what it is for."""
        reg_svc.recheck(self.account)
        exc = RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_LEDGER).first()
        reg_svc.resolve(exc, user=self.treasurer,
                        resolution="Bank error; they reversed it next day.")
        reg_svc.recheck(self.account)
        exc.refresh_from_db()
        self.assertEqual(exc.status, RegisterException.Status.RESOLVED)
        self.assertEqual(exc.resolution, "Bank error; they reversed it next day.")

    def test_matching_is_by_bank_reference_NOT_amount_and_date(self):
        """Two members giving the same amount on the same day is completely
        ordinary. Matching on that would manufacture exactly the false
        reconciliation this check exists to prevent."""
        # same amount, same date, DIFFERENT reference — must NOT be treated as
        # the same event
        self._txn("SOMETHINGELSE", "1000", dt.date(2026, 7, 1))
        reg_svc.recheck(self.account)
        self.assertTrue(RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_LEDGER, ref="UERAAA111").exists())
        self.assertTrue(RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_BANK, ref="SOMETHINGELSE").exists())

    def test_a_bank_transaction_with_no_reference_is_unverifiable_not_an_exception(self):
        """We cannot say the bank disagrees — only that we have no way to ask.
        Calling that a discrepancy would be an accusation the evidence does not
        support."""
        Transaction.objects.create(
            date=dt.date(2026, 7, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("300"), department=self.fund, confirmed=True,
            allocation_status="MANUAL", reference="hand entered")
        reg_svc.recheck(self.account)
        self.assertFalse(RegisterException.objects.filter(
            kind=RegisterException.Kind.MISSING_IN_BANK).exists())
        self.assertEqual(reg_svc.unverifiable(self.account).count(), 1)


class RegisterViewTests(RegisterFixture):

    def test_the_pages_render(self):
        self._import([["2026-07-01", "UERAAA111~x#a~254700~C2B~A", "1000", "", "1000"]])
        for name in ("bank_register", "bank_register_import", "bank_register_exceptions"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_an_auditor_can_read_the_register_but_not_import(self):
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get(reverse("bank_register")).status_code, 200)
        self.assertNotEqual(
            self.client.get(reverse("bank_register_import")).status_code, 200)


# ===========================================================================
# The public application form
# ===========================================================================

class PublicApplicationFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_pub", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        fund = Department.objects.create(
            name="Pub Fund", slug="pub-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Pub Scheme", code="PUB", fund=fund, created_by=self.treasurer)
        BenevolentEventType.objects.create(scheme=self.scheme, name="Bereavement",
                                           code="BER")
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=100),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            household_mode="HOUSEHOLD", created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        cfg = BenevolentSettings.get()
        cfg.public_form_enabled = True
        cfg.save()

    def _submit(self, **overrides):
        c = self.client
        c.get(reverse("benevolent_public_apply"))       # sets the timestamp
        # walk the session clock past the minimum fill time
        s = c.session
        s["ben_apply_ts"] = time.time() - 10
        s.save()
        data = {"scheme": self.scheme.pk, "full_name": "Grace Wanjiku",
                "phone": "254733111222", "standing": "SABBATH_SCHOOL"}
        data.update(overrides)
        return c.post(reverse("benevolent_public_apply"), data)


class PublicFormSecurityTests(PublicApplicationFixture):

    def test_the_form_is_OFF_by_default(self):
        cfg = BenevolentSettings.get()
        cfg.public_form_enabled = False
        cfg.save()
        r = self.client.get(reverse("benevolent_public_apply"))
        self.assertEqual(r.status_code, 404)

    def test_a_honeypot_submission_creates_nothing(self):
        self._submit(website="i-am-a-bot", full_name="Bot")
        self.assertFalse(BenevolentApplication.objects.filter(full_name="Bot").exists())

    def test_a_submission_faster_than_a_human_creates_nothing(self):
        self.client.get(reverse("benevolent_public_apply"))   # ts = now
        r = self.client.post(reverse("benevolent_public_apply"), {
            "scheme": self.scheme.pk, "full_name": "Speedy", "phone": "254700000000",
            "standing": "VISITOR"})
        self.assertFalse(
            BenevolentApplication.objects.filter(full_name="Speedy").exists())

    def test_the_form_exposes_no_member_data(self):
        """Write-only by design: no autocomplete, no lookup, no roll. A public
        form that could search the membership would leak it."""
        Member.objects.create(name="Secret Member", phone="254700999888")
        body = self.client.get(reverse("benevolent_public_apply")).content.decode()
        self.assertNotIn("Secret Member", body)
        self.assertNotIn("member_search", body)


class PublicSubmissionTests(PublicApplicationFixture):

    def test_a_submission_captures_the_three_household_sections(self):
        self._submit(
            spouse_name="Peter Wanjiku", spouse_phone="254733111333",
            child_name=["Ann Wanjiku", "Ben Wanjiku"],
            child_phone=["", ""], child_dob=["2012-06-01", "2015-09-20"],
            parent_name=["Mama Wanjiku"], parent_phone=["254733111444"],
            parent_dob=[""])
        app = BenevolentApplication.objects.get(full_name="Grace Wanjiku")
        rels = sorted(app.dependants.values_list("relationship", flat=True))
        self.assertEqual(rels, ["CHILD", "CHILD", "PARENT", "SPOUSE"])
        self.assertEqual(app.household_size, 5)

    def test_the_applicants_own_standing_is_recorded_as_their_CLAIM(self):
        self._submit(standing="MEMBER")
        app = BenevolentApplication.objects.get(full_name="Grace Wanjiku")
        self.assertEqual(app.standing, BenevolentApplication.Standing.MEMBER)
        self.assertEqual(app.status, BenevolentApplication.Status.PENDING)

    def test_a_dependants_own_phone_is_kept(self):
        """It is what lets a spouse's payment be matched to the family."""
        self._submit(spouse_name="Peter", spouse_phone="254733111333")
        d = ApplicationDependant.objects.get(relationship="SPOUSE")
        self.assertEqual(d.phone, "254733111333")

    def test_an_application_creates_NO_membership_and_NO_cover(self):
        before = SchemeMembership.objects.count()
        self._submit()
        self.assertEqual(SchemeMembership.objects.count(), before)
        app = BenevolentApplication.objects.get(full_name="Grace Wanjiku")
        self.assertIsNone(app.membership)

    def test_a_name_or_phone_that_is_obviously_incomplete_is_refused(self):
        self._submit(full_name="X")
        self.assertFalse(BenevolentApplication.objects.filter(full_name="X").exists())


class ApplicationReviewTests(PublicApplicationFixture):

    def setUp(self):
        super().setUp()
        self._submit(
            spouse_name="Peter Wanjiku", spouse_phone="254733111333",
            child_name=["Ann Wanjiku"], child_phone=[""], child_dob=[""],
            parent_name=["Mama Wanjiku"], parent_phone=[""], parent_dob=[""])
        self.app = BenevolentApplication.objects.get(full_name="Grace Wanjiku")
        self.client.force_login(self.treasurer)

    def test_approving_registers_a_real_membership_with_the_dependants(self):
        r = self.client.post(
            reverse("benevolent_application_detail", args=[self.app.pk]),
            {"action": "approve", "member": "", "note": "Known to the elder."})
        self.assertEqual(r.status_code, 302)
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, BenevolentApplication.Status.APPROVED)
        self.assertIsNotNone(self.app.membership)
        # the dependants became REAL dependants, through the ordinary service
        self.assertEqual(self.app.membership.dependants.count(), 3)
        self.assertEqual(self.app.membership.registration_type, "HOUSEHOLD")

    def test_approving_links_to_an_EXISTING_member_rather_than_duplicating(self):
        """One person, one record. The reviewer picks; the applicant never
        could, because they cannot search the roll."""
        existing = Member.objects.create(name="Grace Wanjiku", phone="254733111222")
        self.client.post(
            reverse("benevolent_application_detail", args=[self.app.pk]),
            {"action": "approve", "member": existing.pk})
        self.app.refresh_from_db()
        self.assertEqual(self.app.membership.member_id, existing.pk)
        self.assertEqual(Member.objects.filter(phone="254733111222").count(), 1)

    def test_the_reviewer_is_shown_candidates_matched_on_phone(self):
        Member.objects.create(name="Grace W", phone="254733111222")
        r = self.client.get(
            reverse("benevolent_application_detail", args=[self.app.pk]))
        self.assertContains(r, "GRACE W")

    def test_rejecting_creates_no_membership(self):
        before = SchemeMembership.objects.count()
        self.client.post(
            reverse("benevolent_application_detail", args=[self.app.pk]),
            {"action": "reject", "note": "Not known to the congregation."})
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, BenevolentApplication.Status.REJECTED)
        self.assertEqual(SchemeMembership.objects.count(), before)

    def test_an_application_cannot_be_approved_twice(self):
        for _ in range(2):
            self.client.post(
                reverse("benevolent_application_detail", args=[self.app.pk]),
                {"action": "approve", "member": ""})
        self.assertEqual(
            SchemeMembership.objects.filter(scheme=self.scheme).count(), 1)

    def test_the_review_screens_need_the_registration_officer_right(self):
        clerk = User.objects.create_user("nobody_r4", password="x")
        self.client.force_login(clerk)
        self.assertNotEqual(
            self.client.get(reverse("benevolent_applications")).status_code, 200)
