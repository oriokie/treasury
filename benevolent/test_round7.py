"""Round 7 — reported issues, informed by the church's own workbook.

Edwin sent the real thing: a working benevolent scheme (BENEVOLENT_2023_Case_50)
and the WhatsApp update a treasurer produces by hand after every case
(CASE_68.docx). Item 6 is built to that document exactly, because that document
is the specification — it is what the congregation already expects to read.
"""
import csv
import datetime as dt
import io
import re
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig, YearEndClose
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentContribution,
                               BenevolentEventType, BenevolentScheme,
                               SchemeDependant, SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import statement as stmt_svc
from cashbook.models import PaymentInstrument
from statements.models import BankAccount, StatementImport
from statements.models_register import RegisterException, StatementLine
from statements.services import importer as imp_svc
from statements.services import register as reg_bank

TODAY = dt.date.today()


def _csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Narration", "Credit", "Debit", "Balance"])
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


# ===========================================================================
# Item 3 — the debit side of the register
# ===========================================================================

class RegisterDebitTests(TestCase):
    """Reported: "the bank statement is working for credits, not for debits."

    Exactly so, and the reason is instructive. M-Pesa gives every CREDIT a
    receipt code — which is why the credit side worked from the first day. But
    the debits a church actually makes are cheques, standing orders and bank
    charges, and a bank identifies those by a cheque number in the narration, or
    by nothing at all.

    So every debit was falling through the "no reference, cannot say" branch and
    never being checked. The credits are gifts arriving, which are pleasant to
    get wrong. The debits are money LEAVING, which is not.
    """

    def setUp(self):
        self.treasurer = User.objects.create_user("t_r7", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.account = BankAccount.objects.create(name="Main", account_number="1")

    def _import(self, rows):
        return reg_bank.import_file(self.account, filename="s.csv",
                                    user=self.treasurer, path_or_bytes=_csv(rows))

    def test_a_cheque_debit_matches_the_cheque_we_actually_wrote(self):
        PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number="000456", payee="Nakuru Furniture",
            amount=Decimal("5000"), source_kind="EXPENSE", status="ISSUED",
            date_issued=dt.date(2026, 7, 2))
        self._import([["2026-07-02", "CHQ 000456 NAKURU FURNITURE", "", "5000", "-5000"]])
        reg_bank.recheck(self.account)
        self.assertFalse(
            RegisterException.objects.filter(
                kind=RegisterException.Kind.MISSING_IN_LEDGER,
                status=RegisterException.Status.OPEN).exists(),
            "a cheque the church actually wrote was reported as unrecorded")

    def test_a_cheque_debit_we_have_NO_record_of_is_flagged(self):
        self._import([["2026-07-02", "CHEQUE NO. 999 UNKNOWN", "", "3000", "-3000"]])
        reg_bank.recheck(self.account)
        self.assertTrue(
            RegisterException.objects.filter(
                kind=RegisterException.Kind.MISSING_IN_LEDGER).exists(),
            "money left the bank on a cheque we have no record of writing — that is "
            "the single most important thing this check can tell a treasurer")

    def test_a_bank_charge_with_NO_reference_at_all_is_still_flagged(self):
        """It used to be silently skipped, because it has no reference. But money
        left the account and our books do not know about it. Saying nothing
        because the bank did not print a reference hides exactly the thing a
        treasurer most needs to see."""
        self._import([["2026-07-03", "MONTHLY LEDGER FEE", "", "50", "-50"]])
        reg_bank.recheck(self.account)
        exc = RegisterException.objects.get(
            kind=RegisterException.Kind.MISSING_IN_LEDGER)
        self.assertEqual(exc.amount, Decimal("-50"))
        self.assertIn("no bank reference", exc.detail)

    def test_an_unreferenced_CREDIT_is_still_left_alone(self):
        """A credit with no reference could be a cash deposit somebody made at the
        counter. We genuinely cannot say, and saying nothing is more honest than
        guessing."""
        self._import([["2026-07-04", "CASH DEPOSIT", "5000", "", "5000"]])
        reg_bank.recheck(self.account)
        self.assertFalse(RegisterException.objects.exists())

    def test_the_cheque_number_is_read_however_the_bank_writes_it(self):
        cases = {
            "CHQ 000456 NAKURU": "456",
            "CHEQUE NO. 999 PAYEE": "999",
            "CHQ.456": "456",
            "CHEQUE NUMBER 1234": "1234",
            "CK #0789 SUPPLIER": "789",
            "MONTHLY LEDGER FEE": "",
        }

        class _Line:
            def __init__(self, t):
                self.raw_narration = t
                self.reference = ""

        for text, expected in cases.items():
            self.assertEqual(reg_bank.cheque_number(_Line(text)), expected, text)


# ===========================================================================
# Item 4 — reversals
# ===========================================================================

class ReversalTests(TestCase):
    """A bank credits the church by mistake and takes it back. Nothing was
    really received — and the importer was posting it as income."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t_rev", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.account = BankAccount.objects.create(name="Main", account_number="1")
        self.fund = Department.objects.create(
            name="R7 Fund", slug="r7-fund", fund_type=Department.FundType.LOCAL)

    def _data(self):
        return _csv([
            ["2026-08-01", "UGIFT11111~x#tithe~254700~C2B~REAL GIVER", "1000", "", "1000"],
            ["2026-08-02", "UERR222222~x#tithe~254701~C2B~BANK ERROR", "5000", "", "6000"],
            ["2026-08-03", "REVERSAL OF WRONG CREDIT UERR222222", "", "5000", "1000"],
        ])

    def test_the_importer_does_not_post_a_reversed_pair_as_income(self):
        """THE bug. A church's books were showing a gift it never received, and
        its income was overstated by the amount of the bank's own mistake."""
        from django.db.models import Sum
        si = StatementImport.objects.create(
            uploaded_by=self.treasurer, filename="r.csv", bank_account=self.account)
        imp_svc.run_import(si, path_or_bytes=self._data(), filename="r.csv")
        si.refresh_from_db()

        self.assertEqual(si.reversals_skipped, 2)
        # exactly ONE transaction was posted — the real gift. Not three.
        self.assertEqual(Transaction.objects.filter(statement_import=si).count(), 1)

        income = (Transaction.objects.confirmed_credits()
                  .filter(statement_import=si)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        self.assertEqual(
            income, Decimal("1000"),
            "income should be the ONE real gift — not 6,000, which would include "
            "the 5,000 the bank credited by mistake and took straight back")

    def test_the_register_still_holds_every_line_the_bank_sent(self):
        """The register's whole contract is to say what the bank said. It does
        not get to decide the bank was wrong — it records both halves, and they
        net out in the running balance exactly as they do on the real statement."""
        reg_bank.import_file(self.account, filename="r.csv", user=self.treasurer,
                             path_or_bytes=self._data())
        self.assertEqual(StatementLine.objects.count(), 3)
        r = reg_bank.running(self.account)
        self.assertEqual(r["closing"], Decimal("1000"))

    def test_the_reversal_debit_is_not_deduplicated_away(self):
        """A bank reversing its own mistake issues the DEBIT under the SAME
        reference as the credit it is undoing — so keying purely on the reference
        deduplicated it away as a 'duplicate', losing the line entirely and
        leaving the register showing money the bank had already taken back."""
        reg_bank.import_file(self.account, filename="r.csv", user=self.treasurer,
                             path_or_bytes=self._data())
        refs = list(StatementLine.objects.values_list("dedup_key", flat=True))
        self.assertIn("UERR222222", refs)        # the credit
        self.assertIn("UERR222222|D", refs)      # the reversing debit — kept

    def test_neither_half_of_a_reversed_pair_is_chased_as_a_discrepancy(self):
        """There is nothing for our books to have recorded, because nothing really
        happened."""
        reg_bank.import_file(self.account, filename="r.csv", user=self.treasurer,
                             path_or_bytes=self._data())
        reg_bank.recheck(self.account)
        flagged = RegisterException.objects.filter(
            ref__startswith="UERR222222",
            status=RegisterException.Status.OPEN).count()
        self.assertEqual(flagged, 0)

    def test_a_gift_and_an_unrelated_payment_that_happen_to_cancel_are_NOT_paired(self):
        """The safety rail. A church that receives 5,000 on Monday and pays a
        5,000 supplier on Tuesday has two perfectly real movements, and silently
        erasing both because they cancel out would be far worse than leaving a
        genuine reversal unrecognised. A narration keyword is REQUIRED."""
        data = _csv([
            ["2026-09-01", "UGIFT99999~x#tithe~254700~C2B~A GIVER", "5000", "", "5000"],
            ["2026-09-02", "CHQ 000777 A SUPPLIER", "", "5000", "0"],
        ])
        si = StatementImport.objects.create(
            uploaded_by=self.treasurer, filename="x.csv", bank_account=self.account)
        imp_svc.run_import(si, path_or_bytes=data, filename="x.csv")
        si.refresh_from_db()
        self.assertEqual(si.reversals_skipped, 0,
                         "two real, unrelated movements were wrongly treated as a "
                         "reversal simply because they cancelled out")


# ===========================================================================
# Item 6 — the case statement (built to CASE_68.docx)
# ===========================================================================

class CaseStatementTests(TestCase):
    """Built to the document the church already produces by hand after every
    case. That document IS the specification — it is what the congregation
    expects to read, and the treasurer was assembling it from a spreadsheet."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t_st", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

        fund = Department.objects.create(name="St Fund", slug="st-fund",
                                         fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent", code="STB", fund=fund, created_by=self.treasurer)
        self.event = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=900),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            registration_required=True, registration_fee=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("50000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        def enrol(name, phone, days_ago):
            return reg_svc.register(
                self.scheme, Member.objects.create(name=name, phone=phone),
                joined_on=TODAY - dt.timedelta(days=days_ago), user=self.treasurer)

        # a previous case, so "new registrations" has a boundary to be since
        self.old = enrol("Old Member", "254700990000", 400)
        case_svc.create_case(self.scheme, event_type=self.event, membership=self.old,
                             event_date=TODAY - dt.timedelta(days=60),
                             user=self.treasurer)

        self.long_standing = [enrol(f"Member {c}", f"25470099{i:04d}", 300)
                              for i, c in enumerate("ABCDEFGH")]
        self.newcomers = [enrol(f"Joiner {c}", f"25470098{i:04d}", 30)
                          for i, c in enumerate("XYZ")]
        for m in self.newcomers:
            contrib_svc.record_fee(m, kind="REGISTRATION", user=self.treasurer,
                                   date=TODAY - dt.timedelta(days=30))

        self.bereaved = self.long_standing[0]
        self.case = case_svc.create_case(
            self.scheme, event_type=self.event, membership=self.bereaved,
            event_date=TODAY - dt.timedelta(days=5), user=self.treasurer)

        # six of the long-standing pay, plus all three newcomers
        self.payers = self.long_standing[1:7] + self.newcomers
        for m in self.payers:
            contrib_svc.record_contribution(
                self.scheme, date=TODAY, amount=Decimal("500"), user=self.treasurer,
                membership=m, case=self.case, kind="LEVY")

    def _data(self):
        return stmt_svc.case_statement(self.case)

    def test_the_counts_add_up_the_way_the_churchs_own_report_does(self):
        d = self._data()
        # 12 on the roll: 8 long-standing + 3 newcomers + the old member
        self.assertEqual(d["registered"], 12)
        self.assertEqual(d["n_contributed"], 9)
        # everyone who did not pay, EXCEPT the bereaved member
        self.assertEqual(d["n_defaulted"], 2)
        self.assertEqual(d["n_contributed"] + d["n_defaulted"] + 1, d["registered"])

    def test_the_bereaved_member_is_never_on_the_defaulters_list(self):
        """They are the reason for the case. Publishing their name as somebody who
        failed to contribute to their own bereavement would be grotesque."""
        d = self._data()
        self.assertNotIn(self.bereaved, d["defaulted"])

    def test_someone_who_joined_AFTER_the_event_is_not_a_defaulter(self):
        """They were never asked to contribute to this case. Membership is counted
        as the case saw it."""
        late = reg_svc.register(
            self.scheme, Member.objects.create(name="Too Late", phone="254700970001"),
            joined_on=TODAY, user=self.treasurer)
        d = self._data()
        self.assertNotIn(late, d["defaulted"])
        self.assertEqual(d["registered"], 12)      # unchanged

    def test_new_registrations_means_since_the_PREVIOUS_case(self):
        """That is what the church means by it, and it is what the registration
        fees on this statement actually relate to."""
        d = self._data()
        self.assertEqual(d["n_new_regs"], 3)
        self.assertEqual({m.pk for m in d["new_regs"]},
                         {m.pk for m in self.newcomers})

    def test_the_money_lines_are_right(self):
        d = self._data()
        self.assertEqual(d["member_contributions"], Decimal("4500"))   # 9 x 500
        self.assertEqual(d["registration_fees"], Decimal("1500"))      # 3 x 500
        self.assertEqual(d["total_contribution"], Decimal("6000"))
        self.assertEqual(d["surplus"], Decimal("6000"))                # no expenses yet

    def test_expenses_reduce_the_surplus(self):
        case_svc.submit_case(self.case, user=self.treasurer)
        case_svc.assess_case(self.case, user=self.treasurer)
        case_svc.approve_case(self.case, amount=Decimal("50000"),
                              user=self.treasurer, allow_self_approval=True,
                              override_reason="Test.")
        payout = case_svc.record_payout(self.case, amount=Decimal("4000"),
                                        user=self.treasurer, payee_name="Family")
        payout.expense.status = "APPROVED"
        payout.expense.approved_by = self.treasurer
        payout.expense.save()

        d = self._data()
        self.assertEqual(d["expenses"], Decimal("4000"))
        self.assertEqual(d["surplus"], Decimal("2000"))

    def test_the_text_is_plain_and_pasteable(self):
        """WhatsApp mangles markdown, box-drawing and most emoji. A treasurer
        pasting a broken table into a congregation group at 10pm is not a problem
        worth creating."""
        text = stmt_svc.as_text(self._data())
        self.assertNotIn("*", text)
        self.assertNotIn("|", text)
        self.assertNotIn("<", text)
        self.assertIn("MEMBERS WHO CONTRIBUTED", text)
        self.assertIn("MEMBERS WHO DID NOT CONTRIBUTE", text)
        self.assertIn("NEW REGISTRATIONS", text)
        self.assertIn("Surplus", text)

    def test_the_page_renders_and_downloads(self):
        r = self.client.get(reverse("benevolent_case_statement", args=[self.case.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Copy for WhatsApp")

        txt = self.client.get(reverse("benevolent_case_statement", args=[self.case.pk]),
                              {"format": "txt"})
        self.assertEqual(txt["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn(b"MEMBERS WHO DID NOT CONTRIBUTE", txt.content)

    def test_the_beneficiarys_RELATIONSHIP_is_on_the_statement(self):
        """"Mzee Harun Kanyi — Father to Grace Nyaboke". That line tells the
        congregation WHOSE loss this is, which is the whole reason anybody is
        being asked to contribute. The church's own records have always carried
        it; we were dropping it whenever the beneficiary was not a registered
        dependant."""
        self.case.beneficiary_name = "Mzee Harun Kanyi"
        self.case.beneficiary_relationship = "Father to Grace Nyaboke"
        self.case.save()
        text = stmt_svc.as_text(self._data())
        self.assertIn("Mzee Harun Kanyi", text)
        self.assertIn("Father to Grace Nyaboke", text)


# ===========================================================================
# Items 5 & 6 — Telegram
# ===========================================================================

class TelegramRegistryTests(CaseStatementTests):
    """The registry, reachable from a phone. Looking a member up is the single
    most common thing a treasurer or an elder is asked at church, and until now
    it needed a laptop."""

    def _strip(self, html):
        return re.sub(r"<[^>]+>", "", html)

    def test_member_lookup(self):
        from core.services.telegram_bot import _do_member
        out = self._strip(_do_member("Member B"))
        self.assertIn("MEMBER B", out.upper())
        self.assertIn("Good standing", out)
        self.assertIn("Owing", out)

    def test_member_lookup_by_phone(self):
        from core.services.telegram_bot import _do_member
        out = self._strip(_do_member("254700990001"))
        self.assertIn("MEMBER B", out.upper())

    def test_member_lookup_says_so_when_nobody_matches(self):
        from core.services.telegram_bot import _do_member
        self.assertIn("Nobody matching", self._strip(_do_member("Zzzz")))

    def test_scheme_overview(self):
        from core.services.telegram_bot import _do_benevolent
        out = self._strip(_do_benevolent())
        self.assertIn("Benevolent", out)
        self.assertIn("Open cases", out)

    def test_case_list_and_statement(self):
        from core.services.telegram_bot import _do_case
        listing = self._strip(_do_case(1, "")["text"])
        self.assertIn(self.case.number, listing)

        reply = _do_case(1, self.case.number)
        body = reply.get("text") or reply.get("caption", "")
        if "document" in reply:
            body = reply["document"].decode()
        self.assertIn("CONTRIBUTED", self._strip(body).upper())

    def test_a_long_statement_is_sent_as_a_FILE_not_a_truncated_message(self):
        """Telegram caps a message at 4096 characters, and a 200-member scheme's
        defaulters list will pass that. Truncating it would cut names off the
        list — the one thing on the statement nobody may quietly drop."""
        for i in range(200):
            reg_svc.register(
                self.scheme,
                Member.objects.create(name=f"Bulk Member {i:03d}",
                                      phone=f"2547001{i:05d}"),
                joined_on=TODAY - dt.timedelta(days=200), user=self.treasurer)
        from core.services.telegram_bot import _do_case
        reply = _do_case(1, self.case.number)
        self.assertIn("document", reply)
        self.assertTrue(reply["filename"].endswith(".txt"))
        self.assertIn(b"MEMBERS WHO DID NOT CONTRIBUTE", reply["document"])

    def test_arrears_list(self):
        from core.services.telegram_bot import _do_arrears
        out = self._strip(_do_arrears(""))
        self.assertTrue("arrears" in out.lower() or "Nobody" in out)

    def test_every_new_command_is_in_the_help(self):
        import core.services.telegram_bot as bot
        for cmd in ("/member", "/case", "/benevolent", "/arrears"):
            self.assertIn(cmd, bot.HELP)


# ===========================================================================
# Item 2 — the budget PNG on a phone
# ===========================================================================

class MobileBudgetPngTests(TestCase):
    """Reported: the PNGs are not mobile friendly; the fonts are too small.

    They were not, in fact, small. The IMAGE was 1180px wide — a desktop table —
    and a phone scales that to fit a ~380px viewport, about a third. So "14pt"
    text actually rendered at roughly 4.5pt, along with everything else in it.

    What matters is the RATIO of text size to image width, because that is what
    survives the scaling.
    """

    def _rows(self):
        return [{"name": "Accommodation", "category": "Evangelism", "note": "Camp",
                 "budget": 50000, "actual": 30000, "variance": 20000, "pct": 60}]

    def test_the_image_is_narrow_enough_for_a_phone(self):
        from cashbook.services import goal_chart as gc
        self.assertLessEqual(
            gc_logical_width(gc), 800,
            "a wider image is scaled down further on a phone, shrinking the text "
            "with it — the width IS the font-size problem")

    def test_the_text_to_width_ratio_is_legible(self):
        from cashbook.services import goal_chart as gc
        png = gc.build_budget_items_png(
            dept_name="Development", year=2026, rows=self._rows(),
            tot_budget=50000, tot_actual=30000, tot_variance=20000)
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        logical_w = img.size[0] // gc.SCALE
        # 19pt body text on a 760px image ≈ 2.5%; it was 14/1180 ≈ 1.2%
        ratio = 19 / logical_w
        self.assertGreater(ratio, 0.02,
                           "text is too small relative to the image width — on a "
                           "phone it will be scaled into illegibility")

    def test_there_is_no_progress_bar(self):
        """A bar is a picture of a number, and a picture of a number does not
        survive being scaled to a third of its size. The number does."""
        import inspect
        from cashbook.services import goal_chart as gc
        src = inspect.getsource(gc.build_budget_items_png)
        self.assertNotIn("rounded_rectangle", src)

    def test_it_still_renders(self):
        from cashbook.services import goal_chart as gc
        png = gc.build_budget_items_png(
            dept_name="Development", year=2026, rows=self._rows(),
            tot_budget=50000, tot_actual=30000, tot_variance=20000)
        self.assertTrue(png.startswith(b"\x89PNG"))


def gc_logical_width(gc):
    from PIL import Image
    png = gc.build_budget_items_png(
        dept_name="X", year=2026, rows=[], tot_budget=0, tot_actual=0, tot_variance=0)
    return Image.open(io.BytesIO(png)).size[0] // gc.SCALE


# ===========================================================================
# Item 1 — the founding balance, on the department form too
# ===========================================================================

class FoundingBalanceOnDepartmentFormTests(TestCase):
    """The budget page was locked against editing a founding balance after a year
    close. The department edit form was the other way in, and had the same hole:
    editing it there silently rewrites that fund's balance in every year the
    church has ever recorded, backwards — including audited ones."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t_fb", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.dept = Department.objects.create(
            name="FB Fund", slug="fb-fund", fund_type=Department.FundType.LOCAL,
            opening_balance=Decimal("1000"))

    def test_before_a_year_close_it_is_editable(self):
        from departments.forms import DepartmentForm
        form = DepartmentForm(instance=self.dept)
        self.assertFalse(form.fields["opening_balance"].disabled)
        self.assertIn("ONE-TIME", form.fields["opening_balance"].help_text)

    def test_after_a_year_close_it_is_locked(self):
        YearEndClose.objects.create(year=TODAY.year - 1, closed_by=self.treasurer,
                                    total_carried=Decimal(0))
        from departments.forms import DepartmentForm
        form = DepartmentForm(instance=self.dept)
        self.assertTrue(form.fields["opening_balance"].disabled)
        self.assertIn("Locked", form.fields["opening_balance"].help_text)

    def test_a_locked_field_cannot_be_changed_by_a_crafted_post(self):
        """Disabling the input is not enough — a stale or crafted POST must not be
        able to rewrite an audited history either. Django's `disabled` ignores
        submitted data for the field, which is exactly the protection wanted."""
        YearEndClose.objects.create(year=TODAY.year - 1, closed_by=self.treasurer,
                                    total_carried=Decimal(0))
        from departments.forms import DepartmentForm
        form = DepartmentForm(
            instance=self.dept,
            data={"name": "FB Fund", "fund_type": "LOCAL", "category": "MINISTRY",
                  "opening_balance": "999999"})
        form.is_valid()
        self.assertEqual(form.cleaned_data["opening_balance"], Decimal("1000"))

    def test_the_first_time_setup_guide_exists(self):
        import pathlib
        guide = pathlib.Path("docs/FIRST_TIME_SETUP.md")
        self.assertTrue(guide.exists())
        text = guide.read_text()
        self.assertIn("Founding balances", text)
        self.assertIn("one-way door", text)
