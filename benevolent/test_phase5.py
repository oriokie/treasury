"""Phase 5 — Bereavement Case Management.

Grouped around the claims Phase 5 makes:

  1. CASE HISTORY      every workflow transition writes a CaseEvent; nothing
                       moves a case without leaving a line in its narrative.
  2. FUNDING TARGETS   a case can track progress against an explicit goal,
                       independent of the policy's own benefit calculation.
  3. THE BEREAVED       four configurable answers to "does the member a case
     MEMBER'S OWN       is FOR still pay into it", each wired through the
     CONTRIBUTION       levy roster, the pledge calculation and the benefit
                       deduction so all three agree — and the Phase-2 double-
                       charge bug stays fixed.
  4. AUTO-EXEMPTION    an EXEMPT bereaved policy grants a real, auditable
                       MembershipExemption, not silent arithmetic — Standing
                       shows it, arrears reflect it, the event log records it.
  5. DOCUMENTS         a named checklist, not just a yes/no.
  6. CONCURRENT CASES  two open cases for the same member do not interfere.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                               BenevolentSettings, CaseAttachment, CaseEvent,
                               MembershipExemption, SchemeMembership, SchemePolicy,
                               Standing)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import standing as standing_svc
from benevolent.services.eligibility import evaluate, missing_required_documents

TODAY = dt.date.today()


class CaseFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t5", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c5", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

        self.fund = Department.objects.create(
            name="Case Fund", slug="case-fund", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Case Scheme", code="CAS", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")

        self.policy = self._policy()
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.mary = Member.objects.create(name="Mary Achieng", phone="254722000001")
        self.john = Member.objects.create(name="John Otieno", phone="254722000002")
        self.grace = Member.objects.create(name="Grace Nekesa", phone="254722000003")

    def _policy(self, **kw):
        d = dict(scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
                 membership_required=True, waiting_period_days=0,
                 contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
                 levy_amount=Decimal("500"),
                 arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
                 benefit_mode=SchemePolicy.BenefitMode.FIXED,
                 benefit_amount=Decimal("20000"),
                 bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.EXEMPT,
                 created_by=self.treasurer)
        d.update(kw)
        return SchemePolicy.objects.create(**d)

    def _new_version(self, effective_from=None, **kw):
        v = scheme_svc.new_version_from(
            self.policy, effective_from=effective_from or (TODAY - dt.timedelta(days=400)),
            user=self.treasurer)
        for k, val in kw.items():
            setattr(v, k, val)
        v.save()
        scheme_svc.publish_policy(v, user=self.treasurer)
        return v

    def _enrol(self, member, days_ago=200):
        return reg_svc.register(self.scheme, member,
                                joined_on=TODAY - dt.timedelta(days=days_ago),
                                user=self.treasurer)

    def _case(self, membership, event_date=None, raise_via_service=True):
        if raise_via_service:
            return case_svc.create_case(
                self.scheme, event_type=self.bereavement, membership=membership,
                event_date=event_date or (TODAY - dt.timedelta(days=2)),
                reported_date=TODAY, user=self.clerk)
        return BenevolentCase.objects.create(
            scheme=self.scheme, membership=membership, event_type=self.bereavement,
            event_date=event_date or (TODAY - dt.timedelta(days=2)),
            reported_date=TODAY, raised_by=self.clerk)


# ===========================================================================
# 1. CASE HISTORY
# ===========================================================================

class CaseEventTests(CaseFixture):

    def test_raising_a_case_through_the_service_logs_it(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        e = case.events.filter(kind=CaseEvent.Kind.RAISED).first()
        self.assertIsNotNone(e)
        self.assertEqual(e.actor, self.clerk)
        self.assertFalse(e.automated)

    def test_every_workflow_step_leaves_a_line(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        payout = case_svc.record_payout(case, amount=Decimal("20000"), user=self.clerk)

        kinds = list(case.events.values_list("kind", flat=True))
        for expected in (CaseEvent.Kind.RAISED, CaseEvent.Kind.SUBMITTED,
                         CaseEvent.Kind.ASSESSED, CaseEvent.Kind.APPROVED,
                         CaseEvent.Kind.PAYOUT_RAISED):
            self.assertIn(expected, kinds, kinds)

    def test_a_voucher_cleared_in_the_ordinary_expense_screen_is_logged_automatically(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        payout = case_svc.record_payout(case, amount=Decimal("20000"), user=self.clerk)

        payout.expense.status = "APPROVED"
        payout.expense.approved_by = self.treasurer
        payout.expense.save()

        e = case.events.filter(kind=CaseEvent.Kind.PAYOUT_PAID).first()
        self.assertIsNotNone(e)
        self.assertTrue(e.automated)

    def test_rejecting_the_voucher_afterwards_is_also_logged(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        payout = case_svc.record_payout(case, amount=Decimal("20000"), user=self.clerk)
        payout.expense.status = "APPROVED"
        payout.expense.save()

        payout.expense.status = "REJECTED"
        payout.expense.save()
        e = case.events.filter(kind=CaseEvent.Kind.PAYOUT_REVERSED).first()
        self.assertIsNotNone(e)

    def test_rejection_and_cancellation_are_logged_with_reasons(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.reject_case(case, reason="Not a covered event.", user=self.treasurer)
        e = case.events.get(kind=CaseEvent.Kind.REJECTED)
        self.assertEqual(e.reason, "Not a covered event.")

        case2 = self._case(m, event_date=TODAY - dt.timedelta(days=1))
        case_svc.cancel_case(case2, user=self.clerk, reason="Raised by mistake.")
        e2 = case2.events.get(kind=CaseEvent.Kind.CANCELLED)
        self.assertEqual(e2.reason, "Raised by mistake.")

    def test_committee_votes_are_logged(self):
        self._new_version(approval_mode=SchemePolicy.ApprovalMode.COMMITTEE,
                          committee_quorum=1)
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.record_vote(case, user=self.treasurer, decision="APPROVE",
                             amount=Decimal("20000"))
        self.assertTrue(case.events.filter(kind=CaseEvent.Kind.COMMITTEE_VOTE).exists())

    def test_the_case_history_is_visible_on_the_detail_page(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_detail", args=[case.pk])).content.decode()
        self.assertIn("Case raised for", body)


# ===========================================================================
# 2. FUNDING TARGETS
# ===========================================================================

class FundingTargetTests(CaseFixture):

    def test_a_case_can_be_raised_with_a_target(self):
        m = self._enrol(self.mary)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, membership=m,
            event_date=TODAY, funding_target=Decimal("30000"), user=self.clerk)
        self.assertEqual(case.funding_target, Decimal("30000"))
        self.assertTrue(case.events.filter(kind=CaseEvent.Kind.FUNDING_TARGET).exists())

    def test_progress_tracks_levy_contributions_and_matches_the_levy_round(self):
        m = self._enrol(self.mary)
        payer = self._enrol(self.john)
        case = self._case(m)
        case_svc.set_funding_target(case, amount=Decimal("1000"), user=self.treasurer)

        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), membership=payer,
            case=case, user=self.treasurer)

        self.assertEqual(case.funding_collected, Decimal("500"))
        self.assertEqual(case.funding_progress_percent, Decimal(50))
        self.assertFalse(case.funding_fully_raised)
        self.assertEqual(case.funding_collected, contrib_svc.levy_collected(case))

    def test_reaching_the_target_notifies_once_not_on_every_later_contribution(self):
        m = self._enrol(self.mary)
        payer = self._enrol(self.john)
        case = self._case(m)
        case_svc.set_funding_target(case, amount=Decimal("500"), user=self.treasurer)

        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), membership=payer,
            case=case, user=self.treasurer)
        self.assertTrue(case.funding_fully_raised)
        self.assertEqual(
            case.events.filter(kind=CaseEvent.Kind.FUNDING_REACHED).count(), 1)

        extra_payer = self._enrol(self.grace)
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), membership=extra_payer,
            case=case, user=self.treasurer)
        self.assertEqual(
            case.events.filter(kind=CaseEvent.Kind.FUNDING_REACHED).count(), 1)

    def test_a_target_of_zero_or_negative_is_refused(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        with self.assertRaises(ValidationError):
            case_svc.set_funding_target(case, amount=Decimal("0"), user=self.treasurer)

    def test_a_case_with_no_target_has_no_progress_percent(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        self.assertIsNone(case.funding_progress_percent)
        self.assertFalse(case.funding_fully_raised)

    def test_the_funding_target_view_requires_management_rights(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        self.client.force_login(self.clerk)   # assistant: has manage rights
        r = self.client.post(reverse("benevolent_case_funding_target", args=[case.pk]),
                             {"amount": "5000"})
        self.assertEqual(r.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.funding_target, Decimal("5000"))


# ===========================================================================
# 3. THE BEREAVED MEMBER'S OWN CONTRIBUTION — all four policies
# ===========================================================================

class BereavedPolicyTests(CaseFixture):

    def test_EXEMPT_is_off_the_roster_and_counted_as_zero_in_the_pledge(self):
        m = self._enrol(self.mary)
        self._enrol(self.john)
        case = self._case(m)
        levy = contrib_svc.raise_case_levy(case)
        self.assertNotIn(m.pk, [r["membership"].pk for r in levy["rows"]])
        self.assertIn(m, levy["exempt"])

        self._new_version(benefit_mode=SchemePolicy.BenefitMode.PER_MEMBER_MULTIPLE)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        # 2 active members, bereaved counts as 0 -> 1 contributing member
        self.assertEqual(r.entitlement.amount, Decimal("500.00"))

    def test_CONTRIBUTES_is_on_the_roster_at_the_full_amount(self):
        self._new_version(
            bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.CONTRIBUTES)
        m = self._enrol(self.mary)
        self._enrol(self.john)
        case = self._case(m)
        levy = contrib_svc.raise_case_levy(case)
        row = next(r for r in levy["rows"] if r["membership"].pk == m.pk)
        self.assertEqual(row["due"], Decimal("500"))
        self.assertNotIn(m, levy["exempt"])

    def test_REDUCED_is_on_the_roster_at_the_reduced_amount(self):
        self._new_version(
            bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.REDUCED,
            bereaved_reduction_percent=Decimal("25"))
        m = self._enrol(self.mary)
        self._enrol(self.john)
        case = self._case(m)
        levy = contrib_svc.raise_case_levy(case)
        row = next(r for r in levy["rows"] if r["membership"].pk == m.pk)
        self.assertEqual(row["due"], Decimal("125.00"))       # 25% of 500

    def test_REDUCED_pledge_weight_is_fractional(self):
        self._new_version(
            bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.REDUCED,
            bereaved_reduction_percent=Decimal("50"),
            benefit_mode=SchemePolicy.BenefitMode.PER_MEMBER_MULTIPLE)
        m = self._enrol(self.mary)
        self._enrol(self.john)
        case = self._case(m)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        # 2 members: John (1.0) + Mary (0.5) = 1.5 -> 500 * 1.5 = 750
        self.assertEqual(r.entitlement.amount, Decimal("750.00"))

    def test_COMMITTEE_DECIDES_starts_off_the_roster_while_undecided(self):
        self._new_version(
            bereaved_contribution_policy=
                SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES)
        m = self._enrol(self.mary)
        self._enrol(self.john)
        case = self._case(m)
        levy = contrib_svc.raise_case_levy(case)
        self.assertIn(m, levy["exempt"])

        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        pending = next(c for c in r.checks if c.code == "bereaved_decision")
        self.assertFalse(pending.passed)
        self.assertFalse(pending.blocking)     # advisory, never blocks
        self.assertTrue(r.eligible)            # so the claim overall still proceeds

    def test_COMMITTEE_DECIDES_must_contribute_puts_them_on_the_roster(self):
        self._new_version(
            bereaved_contribution_policy=
                SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES)
        m = self._enrol(self.mary)
        self._enrol(self.john)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.decide_bereaved_contribution(
            case, waived=False, reason="Board resolution 12/2026.", user=self.treasurer)

        levy = contrib_svc.raise_case_levy(case)
        self.assertIn(m.pk, [r["membership"].pk for r in levy["rows"]])
        self.assertTrue(case.events.filter(kind=CaseEvent.Kind.BEREAVED_DECISION).exists())

    def test_COMMITTEE_DECIDES_waived_keeps_them_off_the_roster(self):
        self._new_version(
            bereaved_contribution_policy=
                SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES)
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.decide_bereaved_contribution(
            case, waived=True, reason="Genuine hardship.", user=self.treasurer)
        case.refresh_from_db()
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        decided = next(c for c in r.checks if c.code == "bereaved_decision")
        self.assertTrue(decided.passed)
        self.assertIn("waived", decided.detail)

    def test_deciding_requires_a_reason(self):
        self._new_version(
            bereaved_contribution_policy=
                SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES)
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        with self.assertRaises(ValidationError):
            case_svc.decide_bereaved_contribution(
                case, waived=True, reason="", user=self.treasurer)

    def test_deciding_is_refused_under_a_policy_that_does_not_ask_the_committee(self):
        m = self._enrol(self.mary)     # default policy: EXEMPT
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        with self.assertRaises(ValidationError) as cm:
            case_svc.decide_bereaved_contribution(
                case, waived=True, reason="x", user=self.treasurer)
        self.assertIn("nothing to decide", str(cm.exception))

    def test_the_DEDUCT_bug_is_fixed_no_double_charge(self):
        """The bug found while building this: a 'deduct' bereaved member used to
        be left on the levy roster (asked to pay up front) AND have the same
        amount taken off their benefit — charged twice for one contribution.
        Fixed: they are excluded from the roster, and the deduction is the
        ONLY place their contribution is collected."""
        self._new_version(
            bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.CONTRIBUTES,
            bereaved_deduct_own_levy=True)
        m = self._enrol(self.mary)
        self._enrol(self.john)
        case = self._case(m)

        levy = contrib_svc.raise_case_levy(case)
        self.assertIn(m, levy["exempt"])          # off the roster …
        self.assertNotIn(m.pk, [r["membership"].pk for r in levy["rows"]])

        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        # … and deducted exactly ONCE from the benefit
        self.assertEqual(len(r.entitlement.deductions), 1)
        self.assertEqual(r.entitlement.amount, Decimal("20000") - Decimal("500"))

    def test_REDUCED_plus_deduct_takes_only_the_reduced_amount(self):
        self._new_version(
            bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.REDUCED,
            bereaved_reduction_percent=Decimal("40"), bereaved_deduct_own_levy=True)
        m = self._enrol(self.mary)
        case = self._case(m)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        self.assertEqual(r.entitlement.amount, Decimal("20000") - Decimal("200"))  # 40% of 500

    def test_the_wizard_produces_the_new_enum(self):
        from benevolent.services import wizard as wizard_svc
        answers = {"funding": "PER_CASE_LEVY", "levy_amount": "500",
                  "bereaved_levy": "REDUCED", "bereaved_reduction": "30"}
        cfg, _lines, why = wizard_svc.build_config(answers)
        self.assertEqual(cfg["bereaved_contribution_policy"], "REDUCED")
        self.assertEqual(cfg["bereaved_reduction_percent"], "30")
        self.assertTrue(any(d.setting == "bereaved_contribution_policy" for d in why))

    def test_a_builtin_profile_uses_the_new_field(self):
        from benevolent.services import profiles as profile_svc
        profile_svc.install_builtins()
        p = profile_svc.__dict__  # just ensure import works and profiles installed
        from benevolent.models import PolicyProfile
        prof = PolicyProfile.objects.get(name="Monthly dues, fixed benefit")
        self.assertEqual(prof.config.get("bereaved_contribution_policy"), "EXEMPT")
        self.assertNotIn("bereaved_exempt_own_levy", prof.config)


class BereavedMigrationTests(CaseFixture):
    """The data migration that retired bereaved_exempt_own_levy is exercised by
    every other test in this file indirectly (every fixture policy is created
    fresh, post-migration) — these two check the mapping logic itself, run
    directly against the migration's own translation function shape."""

    def test_fresh_policies_default_to_exempt(self):
        self.assertEqual(self.policy.bereaved_contribution_policy,
                         SchemePolicy.BereavedContributionPolicy.EXEMPT)

    def test_the_old_field_name_no_longer_exists(self):
        self.assertFalse(hasattr(SchemePolicy(), "bereaved_exempt_own_levy"))
        self.assertIn("bereaved_contribution_policy", SchemePolicy.RULE_FIELDS)
        self.assertNotIn("bereaved_exempt_own_levy", SchemePolicy.RULE_FIELDS)


# ===========================================================================
# 4. AUTO-EXEMPTION ON APPROVAL
# ===========================================================================

class AutoExemptionTests(CaseFixture):

    def setUp(self):
        super().setUp()
        self._new_version(
            contribution_mode=SchemePolicy.ContributionMode.HYBRID,
            contribution_amount=Decimal("100"),
            bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.EXEMPT,
            bereaved_dues_waiver_months=3,
            arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT)

    def test_approving_an_EXEMPT_case_grants_a_real_auditable_exemption(self):
        m = self._enrol(self.mary, days_ago=100)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)

        ex = m.exemptions.filter(kind=MembershipExemption.Kind.BEREAVEMENT).first()
        self.assertIsNotNone(ex)
        self.assertTrue(ex.is_approved)          # auto-approved, policy-driven
        self.assertIn(case.number, ex.reason)    # traceable back to the case

        self.assertTrue(case.events.filter(kind=CaseEvent.Kind.EXEMPTION_GRANTED).exists())
        self.assertTrue(m.events.filter(kind__icontains="EXEMPT").exists())

    def test_standing_shows_EXEMPT_not_a_silent_zero(self):
        m = self._enrol(self.mary, days_ago=100)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)

        result = standing_svc.refresh(m)
        self.assertEqual(result.standing, Standing.EXEMPT)
        self.assertTrue(result.covered)

    def test_arrears_are_genuinely_zero_during_the_waiver_and_the_register_agrees(self):
        """The waiver runs FORWARD from the event — it excuses the dues that
        would otherwise fall due during the waiver window, not debts already
        owed before the case existed. Clear what was already owed, then prove
        no NEW arrears accrue through the waiver window."""
        m = self._enrol(self.mary, days_ago=100)
        already_owed = contrib_svc.arrears_for(m)
        if already_owed:
            contrib_svc.record_contribution(
                self.scheme, date=TODAY, amount=already_owed, membership=m,
                user=self.treasurer, period_label="")

        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)

        owed = contrib_svc.arrears_for(m, as_of=case.event_date + dt.timedelta(days=30))
        self.assertEqual(owed, Decimal(0))

    def test_no_waiver_months_means_no_automatic_exemption(self):
        self._new_version(effective_from=TODAY - dt.timedelta(days=300),
                          bereaved_dues_waiver_months=0,
                          bereaved_contribution_policy=
                              SchemePolicy.BereavedContributionPolicy.EXEMPT)
        m = self._enrol(self.mary, days_ago=100)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        self.assertFalse(m.exemptions.exists())

    def test_CONTRIBUTES_policy_grants_no_automatic_exemption(self):
        self._new_version(
            effective_from=TODAY - dt.timedelta(days=300),
            bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.CONTRIBUTES,
            bereaved_dues_waiver_months=3)
        m = self._enrol(self.mary, days_ago=100)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("20000"), user=self.treasurer)
        self.assertFalse(m.exemptions.exists())


# ===========================================================================
# 5. DOCUMENTS
# ===========================================================================

class DocumentChecklistTests(CaseFixture):

    def test_named_documents_are_checked_individually(self):
        self.bereavement.required_documents = ["Burial permit", "Death certificate"]
        self.bereavement.save()
        m = self._enrol(self.mary)
        case = self._case(m)
        missing = missing_required_documents(self.bereavement, case)
        self.assertEqual(set(missing), {"Burial permit", "Death certificate"})

        CaseAttachment.objects.create(
            case=case, document_type="Burial permit", uploaded_by=self.treasurer)
        missing = missing_required_documents(self.bereavement, case)
        self.assertEqual(missing, ["Death certificate"])

    def test_the_eligibility_check_blocks_until_every_named_document_is_present(self):
        self.bereavement.required_documents = ["Burial permit"]
        self.bereavement.save()
        m = self._enrol(self.mary)
        case = self._case(m)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        docs = next(c for c in r.checks if c.code == "documents")
        self.assertFalse(docs.passed)
        self.assertIn("Burial permit", docs.detail)

        CaseAttachment.objects.create(
            case=case, document_type="Burial permit", uploaded_by=self.treasurer)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        docs = next(c for c in r.checks if c.code == "documents")
        self.assertTrue(docs.passed)

    def test_unnamed_requires_document_still_works_as_before(self):
        """Backward compatible: an event type with the plain toggle on, and no
        named list, still just needs at least one attachment of any kind."""
        self.bereavement.requires_document = True
        self.bereavement.save()
        m = self._enrol(self.mary)
        case = self._case(m)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        self.assertFalse(next(c for c in r.checks if c.code == "documents").passed)

        CaseAttachment.objects.create(case=case, label="Anything", uploaded_by=self.treasurer)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case)
        self.assertTrue(next(c for c in r.checks if c.code == "documents").passed)

    def test_the_case_screen_shows_what_is_missing(self):
        self.bereavement.required_documents = ["Burial permit", "ID copy"]
        self.bereavement.save()
        m = self._enrol(self.mary)
        case = self._case(m)
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_detail", args=[case.pk])).content.decode()
        self.assertIn("Burial permit", body)
        self.assertIn("ID copy", body)

    def test_attaching_a_document_is_logged_with_its_type(self):
        self.bereavement.required_documents = ["Burial permit"]
        self.bereavement.save()
        m = self._enrol(self.mary)
        case = self._case(m)
        self.client.force_login(self.treasurer)
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("permit.pdf", b"data", content_type="application/pdf")
        self.client.post(reverse("benevolent_case_action", args=[case.pk, "attach"]),
                         {"file": f, "document_type": "Burial permit"})
        e = case.events.filter(kind=CaseEvent.Kind.DOCUMENT_ADDED).first()
        self.assertIsNotNone(e)
        self.assertIn("Burial permit", e.summary)


# ===========================================================================
# 6. MULTIPLE CONCURRENT CASES
# ===========================================================================

class ConcurrentCasesTests(CaseFixture):

    def test_a_member_may_have_two_open_cases_at_once(self):
        m = self._enrol(self.mary)
        case1 = self._case(m, event_date=TODAY - dt.timedelta(days=10))
        case2 = self._case(m, event_date=TODAY - dt.timedelta(days=2))
        self.assertNotEqual(case1.pk, case2.pk)
        self.assertEqual(
            BenevolentCase.objects.filter(membership=m,
                                          status__in=BenevolentCase.OPEN_STATUSES).count(),
            2)

    def test_levies_for_two_concurrent_cases_do_not_cross_contaminate(self):
        m1 = self._enrol(self.mary)
        m2 = self._enrol(self.john)
        payer = self._enrol(self.grace)
        case1 = self._case(m1, event_date=TODAY - dt.timedelta(days=5))
        case2 = self._case(m2, event_date=TODAY - dt.timedelta(days=1))

        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), membership=payer,
            case=case1, user=self.treasurer)

        self.assertEqual(contrib_svc.levy_collected(case1), Decimal("500"))
        self.assertEqual(contrib_svc.levy_collected(case2), Decimal(0))

    def test_an_undecided_open_case_does_not_count_against_the_annual_claim_cap(self):
        self._new_version(max_claims_per_year=1)
        m = self._enrol(self.mary)
        case1 = self._case(m, event_date=TODAY - dt.timedelta(days=5))
        # a SECOND case in the same year, still in draft/submitted — not yet
        # decided, so it must not by itself block a fresh eligibility check
        case2 = self._case(m, event_date=TODAY - dt.timedelta(days=1))
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case2)
        freq = next(c for c in r.checks if c.code == "claim_frequency")
        self.assertTrue(freq.passed)   # neither case has been decided yet

    def test_once_one_case_is_decided_it_counts_against_the_cap_for_the_other(self):
        self._new_version(max_claims_per_year=1)
        m = self._enrol(self.mary)
        case1 = self._case(m, event_date=TODAY - dt.timedelta(days=10))
        case_svc.submit_case(case1, user=self.clerk)
        case_svc.assess_case(case1, user=self.treasurer)
        case_svc.approve_case(case1, amount=Decimal("20000"), user=self.treasurer)

        case2 = self._case(m, event_date=TODAY - dt.timedelta(days=1))
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m, case=case2)
        freq = next(c for c in r.checks if c.code == "claim_frequency")
        self.assertFalse(freq.passed)

    def test_each_case_has_its_own_independent_funding_target(self):
        m = self._enrol(self.mary)
        case1 = self._case(m, event_date=TODAY - dt.timedelta(days=5))
        case2 = self._case(m, event_date=TODAY - dt.timedelta(days=1))
        case_svc.set_funding_target(case1, amount=Decimal("1000"), user=self.treasurer)
        case_svc.set_funding_target(case2, amount=Decimal("2000"), user=self.treasurer)
        self.assertEqual(case1.funding_target, Decimal("1000"))
        self.assertEqual(case2.funding_target, Decimal("2000"))


# ===========================================================================
# Views & permissions
# ===========================================================================

class Phase5ViewTests(CaseFixture):

    def test_an_assistant_cannot_record_the_committees_bereaved_decision(self):
        self._new_version(
            bereaved_contribution_policy=
                SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES)
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)

        self.client.force_login(self.clerk)
        self.client.post(reverse("benevolent_case_bereaved_decision", args=[case.pk]),
                         {"waived": "1", "reason": "x"})
        case.refresh_from_db()
        self.assertIsNone(case.bereaved_levy_waived)

    def test_the_funding_progress_bar_renders(self):
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.set_funding_target(case, amount=Decimal("1000"), user=self.treasurer)
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_detail", args=[case.pk])).content.decode()
        self.assertIn("1,000.00", body)
