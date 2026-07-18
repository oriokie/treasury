"""Phase 4 — the Contribution Engine & Intelligent Allocation.

Grouped around the claims Phase 4 makes:

  1. MONEY vs OBLIGATIONS   a penalty is not income; a waiver is not an expense;
                            a refund IS money and a reversal is not. Getting these
                            wrong is how a fund quietly starts reporting money that
                            does not exist.
  2. THE LEDGER             every contribution posts, every refund posts, and
                            nothing else does. Traceable receipt → ledger → report.
  3. ALLOCATION             every identifier the brief asks for, with confidence
                            scoring, and — crucially — the good sense to refuse.
  4. INTAKE                 auto / review / unmatched / duplicate. And the money is
                            NEVER lost, whichever of those happens.
  5. VALIDATIONS            the policy decides what a contribution may even be.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense
from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentContribution,
                               BenevolentEventType, BenevolentScheme,
                               BenevolentSettings, ContributionIntake,
                               ContributionRefund, ContributionRule, MemberAdjustment,
                               RegistrationType, SchemeDependant, SchemeMembership,
                               SchemePolicy, Standing)
from benevolent.services import allocation as alloc_svc
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import engine as engine_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import standing as standing_svc

TODAY = dt.date.today()


class EngineFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t4", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c4", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

        self.fund = Department.objects.create(
            name="Engine Fund", slug="engine-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Engine Scheme", code="ENG", fund=self.fund,
            created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")

        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            registration_fee=Decimal("500"), registration_required=True,
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.mary = Member.objects.create(name="Mary Wanjiru", phone="254711000001")
        self.john = Member.objects.create(name="John Kamau", phone="254711000002")
        self.m = reg_svc.register(self.scheme, self.mary,
                                  joined_on=TODAY - dt.timedelta(days=90),
                                  user=self.treasurer)
        # The obligations engine (built after this fixture) correctly applies an
        # unpaid registration fee before anything else, regardless of what a
        # narration says — real, intended behaviour, but not what THIS fixture's
        # tests are about, so Mary's registration is already settled here.
        self.m.registration_fee_paid = True
        self.m.save(update_fields=["registration_fee_paid"])

    def _bank_receipt(self, *, amount, reference="", phone="", name="",
                      date=None, department=None):
        """A receipt as the statement importer would have created it: banked, in the
        fund, in the ledger — and with nobody yet attached to it."""
        return Transaction.objects.create(
            date=date or TODAY, amount=Decimal(amount),
            department=department or self.fund,
            direction=Transaction.Direction.CREDIT,
            channel=Transaction.Channel.BANK,
            allocation_status=Transaction.Status.AUTO,
            confirmed=True, reference=reference,
            payer_phone=phone, payer_name=name,
            raw_narration=f"{reference} {name} {phone}")


# ===========================================================================
# 1. MONEY vs OBLIGATIONS
# ===========================================================================

class ObligationsAreNotMoneyTests(EngineFixture):

    def test_a_penalty_charged_posts_NOTHING(self):
        """It is not income. Nobody has paid it, and they may never. Recognising it
        as revenue would book money the church does not have."""
        from core.metrics import metrics
        before = metrics.fund_balance(self.fund)

        adj = engine_svc.charge(
            self.m, kind=MemberAdjustment.Kind.PENALTY, amount=Decimal("300"),
            reason="Late payment penalty under rule 7.", user=self.clerk)
        engine_svc.approve_adjustment(adj, user=self.treasurer)

        # nothing moved
        self.assertEqual(metrics.fund_balance(self.fund), before)
        self.assertEqual(Transaction.objects.filter(department=self.fund).count(), 0)
        self.assertEqual(Expense.objects.filter(department=self.fund).count(), 0)
        # but the member owes 300 more
        self.assertEqual(engine_svc.adjustments_total(self.m), Decimal("300"))

    def test_a_waiver_posts_NOTHING_either(self):
        """No money left the church — it simply stopped asking. Booking it as a
        payment would show a cash outflow that never happened, and the cash book
        would stop agreeing with the bank."""
        from core.metrics import metrics
        before = metrics.fund_balance(self.fund)

        adj = engine_svc.waive(
            self.m, amount=Decimal("400"), reason="Hardship — lost his job.",
            user=self.clerk)
        engine_svc.approve_adjustment(adj, user=self.treasurer)

        self.assertEqual(metrics.fund_balance(self.fund), before)
        self.assertEqual(Expense.objects.count(), 0)
        self.assertEqual(engine_svc.adjustments_total(self.m), Decimal("-400"))

    def test_a_penalty_becomes_income_only_when_it_is_PAID(self):
        from core.metrics import metrics
        adj = engine_svc.charge(
            self.m, kind=MemberAdjustment.Kind.PENALTY, amount=Decimal("300"),
            reason="Late payment.", user=self.clerk)
        engine_svc.approve_adjustment(adj, user=self.treasurer)
        self.assertEqual(metrics.fund_balance(self.fund), Decimal(0))

        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("300"), membership=self.m,
            user=self.treasurer, kind=BenevolentContribution.Kind.PENALTY)

        # NOW it is income, and now the fund has it
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("300"))

    def test_penalties_and_waivers_move_what_the_member_OWES(self):
        owed_before = contrib_svc.arrears_for(self.m)
        self.assertGreater(owed_before, Decimal(0))

        p = engine_svc.charge(self.m, kind=MemberAdjustment.Kind.PENALTY,
                              amount=Decimal("300"), reason="Late.", user=self.clerk)
        engine_svc.approve_adjustment(p, user=self.treasurer)
        self.assertEqual(contrib_svc.arrears_for(self.m), owed_before + 300)

        w = engine_svc.waive(self.m, amount=Decimal("100"), reason="Goodwill.",
                             user=self.clerk)
        engine_svc.approve_adjustment(w, user=self.treasurer)
        self.assertEqual(contrib_svc.arrears_for(self.m), owed_before + 300 - 100)

    def test_an_unapproved_adjustment_changes_nothing(self):
        """Proposing that a member be fined does not fine them."""
        owed = contrib_svc.arrears_for(self.m)
        engine_svc.charge(self.m, kind=MemberAdjustment.Kind.PENALTY,
                          amount=Decimal("500"), reason="Late.", user=self.clerk)
        self.assertEqual(contrib_svc.arrears_for(self.m), owed)

    def test_an_adjustment_needs_a_second_person_to_approve_it(self):
        adj = engine_svc.charge(self.m, kind=MemberAdjustment.Kind.PENALTY,
                                amount=Decimal("300"), reason="Late.",
                                user=self.treasurer)
        with self.assertRaises(ValidationError) as cm:
            engine_svc.approve_adjustment(adj, user=self.treasurer)
        self.assertIn("other than the person who proposed", str(cm.exception))

    def test_an_adjustment_must_record_why(self):
        with self.assertRaises(ValidationError):
            engine_svc.charge(self.m, kind=MemberAdjustment.Kind.PENALTY,
                              amount=Decimal("300"), reason="  ", user=self.clerk)

    def test_the_amount_is_always_positive_and_the_KIND_decides_the_sign(self):
        """A signed amount invites a treasurer to type a minus sign and reverse the
        meaning of a penalty by accident."""
        with self.assertRaises(ValidationError):
            engine_svc.charge(self.m, kind=MemberAdjustment.Kind.WAIVER,
                              amount=Decimal("-100"), reason="x", user=self.clerk)

    def test_reversing_an_adjustment_restores_what_was_owed(self):
        owed = contrib_svc.arrears_for(self.m)
        adj = engine_svc.charge(self.m, kind=MemberAdjustment.Kind.PENALTY,
                                amount=Decimal("300"), reason="Late.", user=self.clerk)
        engine_svc.approve_adjustment(adj, user=self.treasurer)
        engine_svc.reverse_adjustment(adj, user=self.treasurer,
                                      reason="Charged in error.")
        self.assertEqual(contrib_svc.arrears_for(self.m), owed)
        adj.refresh_from_db()
        self.assertIsNotNone(adj.reversed_on)      # never deleted


class RefundTests(EngineFixture):

    def test_a_refund_IS_money_and_goes_out_as_an_ordinary_voucher(self):
        from core.metrics import metrics
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("1000"), membership=self.m,
            user=self.treasurer)
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("1000"))

        r = engine_svc.refund(self.m, amount=Decimal("400"),
                              reason="Left the scheme; refund under rule 12.",
                              user=self.clerk)
        exp = r.expense
        self.assertEqual(exp.department, self.fund)
        self.assertEqual(exp.status, Expense.Status.PENDING)   # never self-approved
        self.assertFalse(r.effective)
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("1000"))  # not yet

        exp.status = Expense.Status.APPROVED
        exp.approved_by = self.treasurer
        exp.save()

        r.refresh_from_db()
        self.assertTrue(r.effective)
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("600"))

    def test_a_refund_is_NOT_a_reversal_and_both_facts_stay_in_the_cash_book(self):
        """Reversing a correct receipt to 'cancel out' a refund would hide a real
        payment from the bank reconciliation and understate income AND expenditure."""
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("1000"), membership=self.m,
            user=self.treasurer)
        r = engine_svc.refund(self.m, amount=Decimal("1000"),
                              reason="Full refund on exit.", user=self.clerk)
        r.expense.status = Expense.Status.PAID
        r.expense.save()

        c.refresh_from_db()
        self.assertFalse(c.transaction.is_reversed)     # the receipt STANDS
        self.assertTrue(c.effective)                    # it was real income
        # and the payment out is real too
        self.assertEqual(contrib_svc.contributions_total(membership=self.m),
                         Decimal("1000"))
        self.assertTrue(ContributionRefund.objects.get(pk=r.pk).effective)

    def test_a_refund_cannot_exceed_what_the_member_actually_gave(self):
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("200"), membership=self.m,
            user=self.treasurer)
        with self.assertRaises(ValidationError) as cm:
            engine_svc.refund(self.m, amount=Decimal("5000"), reason="x",
                              user=self.clerk)
        self.assertIn("it is a benefit", str(cm.exception))

    def test_a_refund_must_say_why(self):
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), membership=self.m,
            user=self.treasurer)
        with self.assertRaises(ValidationError):
            engine_svc.refund(self.m, amount=Decimal("100"), reason="", user=self.clerk)

    def test_a_refund_against_a_policy_that_forbids_it_is_RECORDED_not_hidden(self):
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), membership=self.m,
            user=self.treasurer)
        self.assertFalse(self.policy.refund_contributions_on_exit)
        r = engine_svc.refund(self.m, amount=Decimal("100"),
                              reason="Overpayment returned.", user=self.clerk)
        self.assertIn("does not provide for refunds", r.reason)


# ===========================================================================
# 2. THE LEDGER — every contribution traceable, receipt to report
# ===========================================================================

class LedgerTests(EngineFixture):

    def test_every_kind_of_contribution_posts_DR_cash_CR_income(self):
        from ledger.models import JournalLine
        from ledger.services import posting
        posting.ensure_chart()

        kinds = [BenevolentContribution.Kind.DUES,
                 BenevolentContribution.Kind.REGISTRATION,
                 BenevolentContribution.Kind.RENEWAL,
                 BenevolentContribution.Kind.PENALTY,
                 BenevolentContribution.Kind.VOLUNTARY]
        for i, kind in enumerate(kinds):
            c = contrib_svc.record_contribution(
                self.scheme, date=TODAY - dt.timedelta(days=i),
                amount=Decimal("100"), membership=self.m, user=self.treasurer,
                kind=kind, period_label="")
            lines = JournalLine.objects.filter(
                entry__source_type="transaction", entry__source_id=c.transaction_id)
            self.assertTrue(lines.exists(), kind)
            self.assertEqual({l.account.type for l in lines}, {"ASSET", "INCOME"}, kind)

    def test_the_ledger_still_balances_across_the_whole_engine(self):
        from ledger.services import posting
        posting.ensure_chart()
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("2000"), membership=self.m,
            user=self.treasurer)
        adj = engine_svc.charge(self.m, kind=MemberAdjustment.Kind.PENALTY,
                                amount=Decimal("300"), reason="Late.", user=self.clerk)
        engine_svc.approve_adjustment(adj, user=self.treasurer)
        r = engine_svc.refund(self.m, amount=Decimal("500"), reason="Overpaid.",
                              user=self.clerk)
        r.expense.status = Expense.Status.PAID
        r.expense.save()

        self.assertTrue(posting.accounting_equation()["balanced"])

    def test_a_contribution_is_traceable_from_receipt_to_the_fund_statement(self):
        from core.metrics import metrics
        c = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("750"), membership=self.m,
            user=self.treasurer)
        # receipt → contribution → fund balance → the scheme's own figure
        self.assertEqual(c.transaction.department, self.fund)
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("750"))
        self.assertEqual(self.scheme.balance, Decimal("750"))


# ===========================================================================
# 3. ALLOCATION
# ===========================================================================

class AllocationTests(EngineFixture):

    def setUp(self):
        super().setUp()
        ContributionRule.objects.create(
            pattern="eng", match_type=ContributionRule.MatchType.CONTAINS,
            scheme=self.scheme, priority=1)
        self.m2 = reg_svc.register(self.scheme, self.john,
                                   joined_on=TODAY - dt.timedelta(days=90),
                                   user=self.treasurer)

    def test_a_narration_rule_identifies_the_scheme(self):
        scheme, kind, sig = alloc_svc.detect_scheme("eng dues")
        self.assertEqual(scheme, self.scheme)
        self.assertEqual(sig.code, "rule_scheme")

    def test_the_members_own_phone_plus_the_amount_is_enough_to_auto_allocate(self):
        r = alloc_svc.allocate(reference="eng dues", phone="254711000001",
                               name="MARY WANJIRU", amount=Decimal("200"),
                               date=TODAY)
        self.assertEqual(r.best.membership_id, self.m.pk)
        self.assertGreaterEqual(r.confidence, 85)
        codes = [s.code for s in r.best.signals]
        self.assertIn("member_phone", codes)
        self.assertIn("name_exact", codes)
        self.assertIn("amount_dues", codes)

    def test_the_membership_number_is_conclusive(self):
        r = alloc_svc.allocate(reference=f"eng {self.m.number}", amount=Decimal("200"),
                               date=TODAY)
        self.assertEqual(r.best.membership_id, self.m.pk)
        self.assertIn("membership_number", [s.code for s in r.best.signals])
        self.assertGreaterEqual(r.confidence, 85)

    def test_a_case_reference_makes_it_a_levy_for_that_case(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=200),
            user=self.treasurer)
        v2.contribution_mode = SchemePolicy.ContributionMode.HYBRID
        v2.levy_amount = Decimal("500")
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=self.m2, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=5), reported_date=TODAY,
            raised_by=self.clerk)

        r = alloc_svc.allocate(reference=f"eng {case.number}", phone="254711000001",
                               amount=Decimal("500"), date=TODAY)
        self.assertEqual(r.kind, BenevolentContribution.Kind.LEVY)
        self.assertEqual(r.best.case_id, case.pk)
        self.assertEqual(r.best.membership_id, self.m.pk)

    def test_a_household_identifier_finds_the_registration(self):
        self.m.registration_type = RegistrationType.HOUSEHOLD
        self.m.household_name = "The Wanjiru household"
        self.m.save()
        r = alloc_svc.allocate(reference="eng thewanjiruhousehold", amount=Decimal("200"),
                               date=TODAY)
        self.assertEqual(r.best.membership_id, self.m.pk)
        self.assertIn("household_id", [s.code for s in r.best.signals])

    def test_a_SPOUSE_paying_from_their_own_phone_is_recognised(self):
        """Completely routine, and a system that cannot see it drops a perfectly
        normal payment into an unmatched queue every single month."""
        reg_svc.add_dependant(
            self.m, name="Peter Wanjiru",
            relationship=SchemeDependant.Relationship.SPOUSE, user=self.treasurer)
        d = self.m.dependants.first()
        d.phone = "254799999999"
        d.save()

        r = alloc_svc.allocate(reference="eng dues", phone="254799999999",
                               amount=Decimal("200"), date=TODAY)
        self.assertEqual(r.best.membership_id, self.m.pk)
        self.assertIn("spouse_phone", [s.code for s in r.best.signals])

    def test_a_dependant_who_is_a_church_member_is_recognised_by_THEIR_number(self):
        reg_svc.add_dependant(
            self.m, member=self.john,
            relationship=SchemeDependant.Relationship.CHILD, user=self.treasurer)
        r = alloc_svc.allocate(reference="eng dues", phone="254711000002",
                               amount=Decimal("200"), date=TODAY)
        # John has his OWN membership too, so both are candidates — the point is that
        # the household link is SEEN, not that it wins
        codes = {s.code for c in r.candidates for s in c.signals}
        self.assertIn("dependant_phone", codes)

    def test_a_fuzzy_name_is_matched_but_never_carries_an_allocation_alone(self):
        """Two brothers share a surname. A name alone must never attribute money."""
        r = alloc_svc.allocate(reference="eng dues", name="WANJIRU MARY",
                               date=TODAY)
        self.assertEqual(r.best.membership_id, self.m.pk)
        self.assertIn("name_exact", [s.code for s in r.best.signals])
        cfg = BenevolentSettings.get()
        # a name + a scheme rule alone must not clear the auto threshold
        self.assertLess(r.confidence, cfg.auto_allocate_threshold)

    def test_the_narration_names_the_kind_of_money(self):
        kind, sig = alloc_svc.detect_kind("eng registration")
        self.assertEqual(kind, BenevolentContribution.Kind.REGISTRATION)
        kind, _ = alloc_svc.detect_kind("eng levy")
        self.assertEqual(kind, BenevolentContribution.Kind.LEVY)

    def test_two_candidates_a_whisker_apart_is_NOT_confidence(self):
        """Two brothers, one handset, one surname. The allocator must say "I cannot
        tell these apart" rather than pick whichever sorted first — that is exactly
        where a wrong automatic answer is most likely and least likely to be noticed.
        """
        # both members share a phone (a household handset) and similar names
        self.john.name = "Mary Wanjiro"
        self.john.phone = "254711000001"
        self.john.save()

        r = alloc_svc.allocate(reference="eng dues", phone="254711000001",
                               name="MARY WANJIRU", amount=Decimal("200"), date=TODAY)
        self.assertGreaterEqual(len(r.candidates), 2)
        self.assertTrue(r.is_ambiguous)
        self.assertIn("cannot tell them apart", " ".join(r.notes))

    def test_signals_ADD_so_corroboration_is_what_produces_confidence(self):
        weak = alloc_svc.allocate(reference="eng dues", name="MARY WANJIRU", date=TODAY)
        strong = alloc_svc.allocate(reference="eng dues", phone="254711000001",
                                    name="MARY WANJIRU", amount=Decimal("200"),
                                    date=TODAY)
        self.assertGreater(strong.confidence, weak.confidence)
        self.assertGreater(len(strong.best.signals), len(weak.best.signals))

    def test_no_scheme_rule_means_an_honest_blank(self):
        r = alloc_svc.allocate(reference="tithe", phone="254711000001", date=TODAY)
        self.assertIsNone(r.scheme)
        self.assertEqual(r.confidence, 0)
        self.assertIn("No rule or fund identifies this", " ".join(r.notes))


# ===========================================================================
# 4. INTAKE — and the money is never lost
# ===========================================================================

class IntakeTests(EngineFixture):

    def setUp(self):
        super().setUp()
        ContributionRule.objects.create(
            pattern="eng", match_type=ContributionRule.MatchType.CONTAINS,
            scheme=self.scheme, priority=1)

    def test_a_confident_receipt_is_allocated_automatically(self):
        txn = self._bank_receipt(amount="200", reference="eng dues",
                                 phone="254711000001", name="MARY WANJIRU")
        item = engine_svc.intake(txn)
        self.assertEqual(item.status, ContributionIntake.Status.AUTO)
        self.assertIsNotNone(item.contribution)
        self.assertTrue(item.contribution.allocated_automatically)
        self.assertGreaterEqual(item.contribution.allocation_confidence, 85)
        self.assertEqual(item.contribution.membership, self.m)
        self.assertEqual(item.contribution.kind, BenevolentContribution.Kind.DUES)

    def test_an_UNMATCHED_receipt_is_still_banked_and_still_in_the_ledger(self):
        """The claim the whole queue rests on. Allocation is allowed to fail; it is
        never allowed to lose the money."""
        from core.metrics import metrics
        from ledger.services import posting
        posting.ensure_chart()

        txn = self._bank_receipt(amount="750", reference="eng",
                                 phone="254700999999", name="UNKNOWN PERSON")
        item = engine_svc.intake(txn)
        self.assertEqual(item.status, ContributionIntake.Status.UNMATCHED)
        self.assertIsNone(item.contribution)

        # …and yet:
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("750"))
        self.assertTrue(posting.accounting_equation()["balanced"])
        from ledger.models import JournalLine
        self.assertTrue(JournalLine.objects.filter(
            entry__source_type="transaction", entry__source_id=txn.pk).exists())

    def test_an_uncertain_receipt_goes_to_REVIEW_with_its_suggestions(self):
        txn = self._bank_receipt(amount="777", reference="eng",
                                 name="MARY WANJIRU")     # name only: not enough
        item = engine_svc.intake(txn)
        self.assertEqual(item.status, ContributionIntake.Status.REVIEW)
        self.assertEqual(item.suggested_membership, self.m)
        self.assertTrue(item.candidates)

    def test_an_AMBIGUOUS_receipt_is_never_auto_allocated_however_high_the_score(self):
        self.john.name = "Mary Wanjiro"
        self.john.phone = "254711000001"
        self.john.save()
        reg_svc.register(self.scheme, self.john,
                         joined_on=TODAY - dt.timedelta(days=90), user=self.treasurer)

        txn = self._bank_receipt(amount="200", reference="eng dues",
                                 phone="254711000001", name="MARY WANJIRU")
        item = engine_svc.intake(txn)
        self.assertGreaterEqual(item.confidence, 85)      # high…
        self.assertEqual(item.status, ContributionIntake.Status.REVIEW)   # …but queued

    def test_a_possible_duplicate_is_flagged_and_never_auto_allocated(self):
        txn1 = self._bank_receipt(amount="200", reference="eng dues",
                                  phone="254711000001", name="MARY WANJIRU")
        engine_svc.intake(txn1)

        txn2 = self._bank_receipt(amount="200", reference="eng dues",
                                  phone="254711000001", name="MARY WANJIRU")
        item = engine_svc.intake(txn2)
        self.assertEqual(item.status, ContributionIntake.Status.DUPLICATE)
        self.assertIsNotNone(item.duplicate_of)
        self.assertIsNone(item.contribution)
        self.assertIn("same money counted twice", item.note)

    def test_a_treasurer_resolves_a_queue_item_and_the_receipt_does_not_move(self):
        from core.metrics import metrics
        txn = self._bank_receipt(amount="500", reference="eng",
                                 name="SOMEBODY ELSE")
        item = engine_svc.intake(txn)
        before = metrics.fund_balance(self.fund)

        c = engine_svc.resolve(item, membership=self.m,
                               kind=BenevolentContribution.Kind.REGISTRATION,
                               user=self.treasurer)
        item.refresh_from_db()
        self.assertEqual(item.status, ContributionIntake.Status.RESOLVED)
        self.assertEqual(c.membership, self.m)
        self.assertFalse(c.allocated_automatically)
        self.assertEqual(c.transaction, txn)                 # the SAME receipt
        self.assertEqual(metrics.fund_balance(self.fund), before)   # money unmoved

    def test_rejecting_a_queue_item_does_NOT_make_the_money_disappear(self):
        """Deciding a receipt is not benevolent money is a statement about
        ATTRIBUTION, not about whether the church received it. Conflating the two
        would let a treasurer make money vanish from the cash book by clicking a
        button."""
        from core.metrics import metrics
        txn = self._bank_receipt(amount="900", reference="eng", name="MYSTERY")
        item = engine_svc.intake(txn)
        engine_svc.reject(item, user=self.treasurer, note="This is a tithe.")

        item.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(item.status, ContributionIntake.Status.REJECTED)
        self.assertFalse(txn.is_reversed)
        self.assertEqual(metrics.fund_balance(self.fund), Decimal("900"))

    def test_switching_auto_allocation_off_sends_everything_to_review(self):
        cfg = BenevolentSettings.get()
        cfg.auto_allocate = False
        cfg.save()
        txn = self._bank_receipt(amount="200", reference="eng dues",
                                 phone="254711000001", name="MARY WANJIRU")
        item = engine_svc.intake(txn)
        self.assertEqual(item.status, ContributionIntake.Status.REVIEW)

    def test_intake_is_idempotent(self):
        txn = self._bank_receipt(amount="200", reference="eng dues",
                                 phone="254711000001", name="MARY WANJIRU")
        a = engine_svc.intake(txn)
        b = engine_svc.intake(txn)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(BenevolentContribution.objects.count(), 1)

    def test_a_rule_is_only_PROPOSED_never_switched_on_by_itself(self):
        """A rule that silently started routing money because a treasurer happened to
        allocate three receipts the same way is a rule nobody agreed to."""
        for i in range(3):
            txn = self._bank_receipt(amount="200", reference="wlfr",
                                     name="X", date=TODAY - dt.timedelta(days=i))
            item = ContributionIntake.objects.create(
                transaction=txn, scheme=self.scheme,
                status=ContributionIntake.Status.RESOLVED)
        txn = self._bank_receipt(amount="200", reference="wlfr", name="X")
        item = ContributionIntake.objects.create(
            transaction=txn, scheme=self.scheme,
            status=ContributionIntake.Status.REVIEW)
        engine_svc.resolve(item, membership=self.m,
                           kind=BenevolentContribution.Kind.DUES, user=self.treasurer)

        rule = ContributionRule.objects.filter(pattern="wlfr").first()
        self.assertIsNotNone(rule)
        self.assertFalse(rule.active)          # proposed, NOT switched on
        self.assertEqual(rule.source, "LEARNED")


# ===========================================================================
# 5. POLICY-DRIVEN VALIDATION
# ===========================================================================

class ValidationTests(EngineFixture):

    def test_dues_cannot_be_receipted_to_a_scheme_that_has_none(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=200),
            user=self.treasurer)
        v2.contribution_mode = SchemePolicy.ContributionMode.VOLUNTARY
        v2.arrears_treatment = SchemePolicy.ArrearsTreatment.IGNORE
        v2.arrears_block = False
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        problems = engine_svc.validate(
            self.scheme, kind=BenevolentContribution.Kind.DUES,
            membership=self.m, amount=Decimal("200"))
        self.assertTrue(any("no periodic dues" in p for p in problems))

    def test_a_levy_must_say_which_case(self):
        problems = engine_svc.validate(
            self.scheme, kind=BenevolentContribution.Kind.LEVY,
            membership=self.m, amount=Decimal("500"))
        self.assertTrue(any("which case" in p for p in problems))

    def test_a_fee_cannot_be_charged_where_the_policy_charges_none(self):
        problems = engine_svc.validate(
            self.scheme, kind=BenevolentContribution.Kind.RENEWAL,
            membership=self.m, amount=Decimal("300"))
        self.assertTrue(any("no renewal fee" in p for p in problems))

    def test_a_withdrawn_member_owes_nothing_so_their_money_is_a_donation(self):
        reg_svc.withdraw(self.m, user=self.treasurer, reason="Left the church.")
        problems = engine_svc.validate(
            self.scheme, kind=BenevolentContribution.Kind.DUES,
            membership=self.m, amount=Decimal("200"))
        self.assertTrue(any("donation, not a contribution" in p for p in problems))

    def test_the_bereaved_member_is_not_levied_for_their_own_case(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=200),
            user=self.treasurer)
        v2.contribution_mode = SchemePolicy.ContributionMode.HYBRID
        v2.levy_amount = Decimal("500")
        v2.bereaved_contribution_policy = SchemePolicy.BereavedContributionPolicy.EXEMPT
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=self.m, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=5), reported_date=TODAY,
            raised_by=self.clerk)
        problems = engine_svc.validate(
            self.scheme, kind=BenevolentContribution.Kind.LEVY,
            membership=self.m, case=case, amount=Decimal("500"))
        self.assertTrue(any("bereaved member" in p for p in problems))

    def test_a_membership_from_the_wrong_scheme_is_refused(self):
        other_fund = Department.objects.create(
            name="Other Fund", slug="other-fund",
            fund_type=Department.FundType.LOCAL)
        other = BenevolentScheme.objects.create(name="Other", code="OTH",
                                                fund=other_fund)
        pol = SchemePolicy.objects.create(
            scheme=other, effective_from=TODAY - dt.timedelta(days=100),
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("1000"))
        scheme_svc.publish_policy(pol, user=self.treasurer)
        scheme_svc.activate_scheme(other, user=self.treasurer)

        problems = engine_svc.validate(
            other, kind=BenevolentContribution.Kind.DUES, membership=self.m,
            amount=Decimal("200"))
        self.assertTrue(any("belongs to" in p and "ENG" in p for p in problems),
                        problems)

    def test_resolving_a_queue_item_into_an_invalid_contribution_is_refused(self):
        ContributionRule.objects.create(
            pattern="eng", match_type=ContributionRule.MatchType.CONTAINS,
            scheme=self.scheme)
        txn = self._bank_receipt(amount="300", reference="eng", name="X")
        item = engine_svc.intake(txn)
        with self.assertRaises(ValidationError):
            engine_svc.resolve(item, membership=self.m,
                               kind=BenevolentContribution.Kind.RENEWAL,
                               user=self.treasurer)


# ===========================================================================
# Views
# ===========================================================================

class EngineViewTests(EngineFixture):

    def setUp(self):
        super().setUp()
        ContributionRule.objects.create(
            pattern="eng", match_type=ContributionRule.MatchType.CONTAINS,
            scheme=self.scheme)
        txn = self._bank_receipt(amount="777", reference="eng", name="MARY WANJIRU")
        self.item = engine_svc.intake(txn)

    def test_the_engine_screens_load(self):
        self.client.force_login(self.treasurer)
        for url in [reverse("benevolent_intake_queue"),
                    reverse("benevolent_intake_item", args=[self.item.pk]),
                    reverse("benevolent_rules"),
                    reverse("benevolent_allocation_test"),
                    reverse("benevolent_membership_detail", args=[self.m.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_the_allocation_tester_shows_the_signals(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_allocation_test"),
                            {"reference": "eng dues", "phone": "254711000001",
                             "name": "MARY WANJIRU", "amount": "200"})
        body = r.content.decode()
        self.assertIn("Paid from the member&#x27;s own number", body)
        self.assertIn("MARY WANJIRU", body)

    def test_an_assistant_cannot_approve_an_adjustment(self):
        adj = engine_svc.charge(self.m, kind=MemberAdjustment.Kind.WAIVER,
                                amount=Decimal("100"), reason="Goodwill.",
                                user=self.treasurer)
        self.client.force_login(self.clerk)
        self.client.post(
            reverse("benevolent_adjustment_decision", args=[adj.pk, "approve"]))
        adj.refresh_from_db()
        self.assertIsNone(adj.approved_by)

    def test_resolving_through_the_view_attributes_the_money(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_intake_item", args=[self.item.pk]),
            {"membership": self.m.pk, "kind": BenevolentContribution.Kind.VOLUNTARY})
        self.assertEqual(r.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ContributionIntake.Status.RESOLVED)
        self.assertEqual(self.item.contribution.membership, self.m)
