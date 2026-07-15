"""Round 9, items 6/7/8 — the obligations engine.

Items 6, 7 and 8 are one concept seen from three angles: a member owes the
scheme things, in a definite order (registration, then case levies oldest-first),
and a payment is applied down that list. This module tests:

  * the obligations ledger — what a member owes, in priority order (item 6);
  * applying a payment across obligations, splitting the TRANSACTION so a single
    receipt can settle two or three cases in arrears (item 7);
  * auto-allocation of an exact-amount payment when there is one open case, with
    an already-paid payment going to review instead (item 8);
  * the guard: an obligation-amount match must not, on its own, push a name-only
    identity match over the auto-allocate threshold.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentContribution,
                               BenevolentEventType, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import obligations as ob_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class LevySchemeFixture(TestCase):
    """A per-case-levy scheme with a registration fee — the shape where
    obligations actually stack up."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t_ob", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])

        fund = Department.objects.create(name="OB Fund", slug="ob-fund",
                                         fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent", code="OBB", fund=fund, created_by=self.treasurer)
        self.event = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER",
            triggers_on_death=True)
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=900),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            registration_required=True, registration_fee=Decimal("300"),
            benefit_mode=SchemePolicy.BenefitMode.POOLED,
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        self.policy = self.scheme.current_policy

    def enrol(self, name, *, paid_registration=True, days_ago=400):
        m = reg_svc.register(
            self.scheme, Member.objects.create(name=name),
            joined_on=TODAY - dt.timedelta(days=days_ago), user=self.treasurer)
        if paid_registration:
            m.registration_fee_paid = True
            m.save(update_fields=["registration_fee_paid"])
        return m

    def open_case(self, *, event_days_ago, beneficiary="Someone"):
        m = self.enrol(f"Bereaved {event_days_ago}")
        return case_svc.create_case(
            self.scheme, event_type=self.event,
            event_date=TODAY - dt.timedelta(days=event_days_ago),
            membership=m, beneficiary_name=beneficiary, user=self.treasurer)


class ObligationsLedgerTests(LevySchemeFixture):
    def test_registration_fee_comes_first(self):
        m = self.enrol("UNPAID REG", paid_registration=False)
        obs = ob_svc.obligations_for(m)
        self.assertTrue(obs)
        self.assertEqual(obs[0].kind, BenevolentContribution.Kind.REGISTRATION)
        self.assertEqual(obs[0].outstanding, Decimal("300"))

    def test_case_levies_are_ordered_oldest_case_first(self):
        old = self.open_case(event_days_ago=90, beneficiary="Old")
        new = self.open_case(event_days_ago=10, beneficiary="New")
        payer = self.enrol("LEVY PAYER")

        obs = ob_svc.obligations_for(payer)
        levies = [o for o in obs
                  if o.kind == BenevolentContribution.Kind.LEVY]
        self.assertEqual(len(levies), 2)
        # oldest event date first
        self.assertEqual(levies[0].case.pk, old.pk)
        self.assertEqual(levies[1].case.pk, new.pk)

    def test_settled_obligations_drop_out(self):
        case = self.open_case(event_days_ago=30)
        payer = self.enrol("PAYS ONE")
        # pay the levy in full
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), user=self.treasurer,
            membership=payer, case=case, channel="CASH")
        obs = ob_svc.obligations_for(payer)
        levies = [o for o in obs if o.kind == BenevolentContribution.Kind.LEVY]
        self.assertEqual(levies, [], "a fully-paid levy should not still be owed")


class ApplyPaymentTests(LevySchemeFixture):
    def test_one_payment_clears_two_cases_in_arrears(self):
        old = self.open_case(event_days_ago=90, beneficiary="Old")
        new = self.open_case(event_days_ago=20, beneficiary="New")
        payer = self.enrol("ARREARS PAYER")

        # a single 1000 payment should clear both 500 levies, oldest first
        txn = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("1000"),
            user=self.treasurer, membership=payer,
            channel="CASH", kind=BenevolentContribution.Kind.VOLUNTARY).transaction
        # undo that auto-index so we can apply through the obligations engine
        BenevolentContribution.objects.filter(transaction=txn).delete()

        contributions = ob_svc.apply_payment_to_obligations(
            txn, payer, user=self.treasurer)

        self.assertEqual(len(contributions), 2)
        # both levies now fully paid
        self.assertEqual(contrib_svc.levy_paid_by(payer, old), Decimal("500"))
        self.assertEqual(contrib_svc.levy_paid_by(payer, new), Decimal("500"))
        # each contribution is a LEVY of 500 (the transaction was split)
        for c in contributions:
            self.assertEqual(c.kind, BenevolentContribution.Kind.LEVY)
            self.assertEqual(c.amount, Decimal("500"))

    def test_overpayment_beyond_obligations_becomes_voluntary(self):
        case = self.open_case(event_days_ago=30)
        payer = self.enrol("OVERPAYER")

        txn = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("800"),
            user=self.treasurer, membership=payer,
            channel="CASH", kind=BenevolentContribution.Kind.VOLUNTARY).transaction
        BenevolentContribution.objects.filter(transaction=txn).delete()

        contributions = ob_svc.apply_payment_to_obligations(
            txn, payer, user=self.treasurer)

        kinds = sorted(c.kind for c in contributions)
        self.assertIn(BenevolentContribution.Kind.LEVY, kinds)
        self.assertIn(BenevolentContribution.Kind.VOLUNTARY, kinds)
        levy = next(c for c in contributions
                    if c.kind == BenevolentContribution.Kind.LEVY)
        over = next(c for c in contributions
                    if c.kind == BenevolentContribution.Kind.VOLUNTARY)
        self.assertEqual(levy.amount, Decimal("500"))
        self.assertEqual(over.amount, Decimal("300"))

    def test_treasurer_can_target_specific_obligations(self):
        old = self.open_case(event_days_ago=90, beneficiary="Old")
        new = self.open_case(event_days_ago=20, beneficiary="New")
        payer = self.enrol("TARGETED")

        obs = ob_svc.obligations_for(payer)
        new_levy = next(o for o in obs if o.case and o.case.pk == new.pk)
        key = ob_svc.obligation_key(new_levy)

        txn = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"),
            user=self.treasurer, membership=payer,
            channel="CASH", kind=BenevolentContribution.Kind.VOLUNTARY).transaction
        BenevolentContribution.objects.filter(transaction=txn).delete()

        # pay ONLY the newer case, skipping the older one
        ob_svc.apply_payment_to_obligations(
            txn, payer, user=self.treasurer, targets=[key])

        self.assertEqual(contrib_svc.levy_paid_by(payer, new), Decimal("500"))
        self.assertEqual(contrib_svc.levy_paid_by(payer, old), Decimal("0"))

    def test_split_transaction_amounts_do_not_double_count(self):
        """The split divides the transaction, so total across contributions
        equals the original — never more (the property-reads-transaction trap)."""
        old = self.open_case(event_days_ago=90)
        new = self.open_case(event_days_ago=20)
        payer = self.enrol("NO DOUBLE COUNT")

        txn = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("1000"),
            user=self.treasurer, membership=payer,
            channel="CASH", kind=BenevolentContribution.Kind.VOLUNTARY).transaction
        BenevolentContribution.objects.filter(transaction=txn).delete()

        contributions = ob_svc.apply_payment_to_obligations(
            txn, payer, user=self.treasurer)
        total = sum((c.amount for c in contributions), Decimal(0))
        self.assertEqual(total, Decimal("1000"))


class AutoAllocateIntakeTests(LevySchemeFixture):
    """Item 8 — a bank receipt from an identified member, one open case."""

    def _bank_txn(self, member, amount, *, phone=""):
        from giving.models import Transaction
        return Transaction.objects.create(
            date=TODAY, channel=Transaction.Channel.BANK,
            direction=Transaction.Direction.CREDIT, amount=Decimal(amount),
            department=self.scheme.fund, member=member,
            allocation_status=Transaction.Status.REVIEW,
            payer_name=member.name, payer_phone=phone,
            reference=f"{self.scheme.code} levy", confirmed=True,
            raw_narration=f"{self.scheme.code} levy {member.name}")

    def test_single_open_case_exact_levy_auto_allocates(self):
        from benevolent.services import engine as engine_svc
        case = self.open_case(event_days_ago=30)
        payer = self.enrol("PHONE PAYER")
        payer.member.phone = "254722111222"
        payer.member.save()

        txn = self._bank_txn(payer.member, "500", phone="254722111222")
        item = engine_svc.intake(txn, scheme=self.scheme)

        self.assertEqual(item.status, "AUTO")
        self.assertEqual(contrib_svc.levy_paid_by(payer, case), Decimal("500"))

    def test_already_paid_goes_to_review_not_double_posted(self):
        from benevolent.services import engine as engine_svc
        case = self.open_case(event_days_ago=30)
        payer = self.enrol("ALREADY PAID")
        payer.member.phone = "254722333444"
        payer.member.save()
        # they already paid their levy
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), user=self.treasurer,
            membership=payer, case=case, channel="CASH")

        txn = self._bank_txn(payer.member, "500", phone="254722333444")
        item = engine_svc.intake(txn, scheme=self.scheme)

        # caught as an open queue item (DUPLICATE or REVIEW) — never posted twice
        self.assertIn(item.status, ["REVIEW", "DUPLICATE"])
        self.assertTrue(item.is_open)
        # not posted twice: still only the one 500 levy
        self.assertEqual(contrib_svc.levy_paid_by(payer, case), Decimal("500"))


class IdentityGuardTests(LevySchemeFixture):
    """The guard: an obligation-amount match must not push a name-only identity
    match over the auto-allocate threshold."""

    def test_amount_match_does_not_raise_identity_confidence(self):
        from benevolent.services import allocation as alloc

        case = self.open_case(event_days_ago=30)
        # two members with the SAME surname — a name match is ambiguous anyway,
        # but the point is the AMOUNT signal must not count toward identity
        m1 = self.enrol("JAMES OMONDI")
        m2 = self.enrol("PETER OMONDI")

        # a payment quoting only a shared surname and the exact levy amount
        result = alloc.allocate(
            reference=f"{self.scheme.code} levy", name="OMONDI",
            amount=Decimal("500"), date=TODAY, scheme=self.scheme)

        if result.best is not None:
            # whatever the total score, identity confidence excludes the amount
            self.assertLessEqual(
                result.identity_confidence, result.confidence)
            # a name-only signal is below the auto threshold; the amount match
            # must not have lifted identity to/over it
            self.assertLess(result.identity_confidence, 85,
                            "an amount match lifted a name-only guess over the "
                            "auto-allocate threshold")

    def test_identity_score_excludes_amount_signals(self):
        from benevolent.services.allocation import (Candidate, Signal, WEIGHTS,
                                                    _IDENTITY_SIGNALS)
        c = Candidate(member_name="X")
        c.signals.append(Signal("name_fuzzy", "name", WEIGHTS["name_fuzzy"]))
        c.signals.append(Signal("amount_levy", "amount", WEIGHTS["amount_levy"]))
        # total counts both; identity counts only the name
        self.assertEqual(c.score, WEIGHTS["name_fuzzy"] + WEIGHTS["amount_levy"])
        self.assertEqual(c.identity_score, WEIGHTS["name_fuzzy"])


class ReviewQueueAssignmentTests(LevySchemeFixture):
    """Item 7 — a treasurer assigns a queued payment across obligations,
    including two or three cases in arrears at once."""

    def _queued(self, amount, member):
        """Put a payment in the review queue for `member`."""
        from giving.models import Transaction
        from benevolent.models import ContributionIntake
        txn = Transaction.objects.create(
            date=TODAY, channel=Transaction.Channel.BANK,
            direction=Transaction.Direction.CREDIT, amount=Decimal(amount),
            department=self.scheme.fund, member=member.member,
            allocation_status=Transaction.Status.REVIEW,
            payer_name=member.member.name, confirmed=True,
            raw_narration="ambiguous")
        return ContributionIntake.objects.create(
            transaction=txn, scheme=self.scheme,
            status=ContributionIntake.Status.REVIEW,
            suggested_membership=member)

    def test_treasurer_clears_three_cases_in_arrears_in_one_go(self):
        from benevolent.services import engine as engine_svc
        c1 = self.open_case(event_days_ago=90)
        c2 = self.open_case(event_days_ago=60)
        c3 = self.open_case(event_days_ago=30)
        payer = self.enrol("THREE ARREARS")

        item = self._queued("1500", payer)
        contributions = engine_svc.resolve_to_obligations(
            item, membership=payer, user=self.treasurer)

        self.assertEqual(len(contributions), 3)
        item.refresh_from_db()
        self.assertEqual(item.status, "RESOLVED")
        for c in (c1, c2, c3):
            self.assertEqual(contrib_svc.levy_paid_by(payer, c), Decimal("500"))

    def test_view_applies_obligations(self):
        from django.urls import reverse
        c1 = self.open_case(event_days_ago=60)
        c2 = self.open_case(event_days_ago=30)
        payer = self.enrol("VIEW PAYER")
        item = self._queued("1000", payer)

        self.client.force_login(self.treasurer)
        url = reverse("benevolent_intake_item", args=[item.pk])
        resp = self.client.post(url, {
            "apply_obligations": "1",
            "obligation_membership": payer.pk}, follow=True)

        self.assertEqual(resp.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, "RESOLVED")
        self.assertEqual(contrib_svc.levy_paid_by(payer, c1), Decimal("500"))
        self.assertEqual(contrib_svc.levy_paid_by(payer, c2), Decimal("500"))
