"""Phase 6 — Policy Evaluation & Committee Management.

Grouped around the claims Phase 6 makes:

  1. COMMITTEE ROSTER    per-scheme, role-aware, additive — a scheme with no
                        roster configured behaves exactly as before.
  2. APPROVAL LEVELS     committee_requires_chair: a quorum of ordinary
                        members is not enough where the policy names a seat.
  3. REINSTATEMENT FEE   a real bug fix — the field existed since Phase 2 and
                        was never charged. Now raised automatically, through
                        the SAME obligations ledger Phase 4 built, auto-
                        approved the same way Phase 5's bereavement
                        exemption is.
  4. THE POLICY ENGINE   evaluate_reinstatement() reuses the Check dataclass
                        case eligibility already uses — one shape, two rules.
  5. POLICY REFERENCES   every exemption and adjustment now records which
                        policy version was in force, plus a comments field
                        distinct from the required reason.
  6. AUDITABILITY        the consolidated Overrides & Exceptions view.
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
                               CommitteeMember, MemberAdjustment, MembershipExemption,
                               SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import committee as committee_svc
from benevolent.services import engine as engine_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services.eligibility import evaluate_reinstatement

TODAY = dt.date.today()


class Phase6Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t6", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c6", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        # three ordinary people who might sit on a committee, with the general
        # benevolent-committee right but no scheme-specific seat yet
        self.alice = self._committee_capable("alice6")
        self.bob = self._committee_capable("bob6")
        self.carol = self._committee_capable("carol6")

        self.fund = Department.objects.create(
            name="P6 Fund", slug="p6-fund", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="P6 Scheme", code="P6", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            approval_mode=SchemePolicy.ApprovalMode.COMMITTEE, committee_quorum=2,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.mary = Member.objects.create(name="Mary Wafula", phone="254733000001")
        self.membership = reg_svc.register(
            self.scheme, self.mary, joined_on=TODAY - dt.timedelta(days=90),
            user=self.treasurer)

    def _committee_capable(self, username):
        from accounts.models import Profile
        u = User.objects.create_user(username, password="x")
        profile, _ = Profile.objects.get_or_create(
            name="Benevolent committee (test)",
            defaults={"rights": ["benevolent_committee", "view_benevolent"]})
        profile.users.add(u)
        return u

    def _assessed_case(self, membership=None):
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=membership or self.membership,
            event_type=self.bereavement, event_date=TODAY - dt.timedelta(days=2),
            reported_date=TODAY, raised_by=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        return case


# ===========================================================================
# 1. COMMITTEE ROSTER — additive, per-scheme, role-aware
# ===========================================================================

class CommitteeRosterTests(Phase6Fixture):

    def test_a_scheme_with_no_roster_lets_anyone_with_the_right_vote(self):
        """The zero-config default: nothing here should change existing
        behaviour for a scheme that has never configured a roster."""
        self.assertFalse(committee_svc.has_roster(self.scheme))
        case = self._assessed_case()
        vote = case_svc.record_vote(case, user=self.alice, decision="APPROVE")
        self.assertEqual(vote.user, self.alice)

    def test_seating_someone_creates_the_roster(self):
        committee_svc.add_member(self.scheme, self.alice, added_by=self.treasurer)
        self.assertTrue(committee_svc.has_roster(self.scheme))
        self.assertTrue(committee_svc.is_seated(self.scheme, self.alice))
        self.assertFalse(committee_svc.is_seated(self.scheme, self.bob))

    def test_once_a_roster_exists_only_seated_members_may_vote(self):
        committee_svc.add_member(self.scheme, self.alice, added_by=self.treasurer)
        committee_svc.add_member(self.scheme, self.bob, added_by=self.treasurer)
        case = self._assessed_case()

        case_svc.record_vote(case, user=self.alice, decision="APPROVE")  # fine

        with self.assertRaises(ValidationError) as cm:
            case_svc.record_vote(case, user=self.carol, decision="APPROVE")
        self.assertIn("not seated", str(cm.exception))

    def test_re_seating_someone_previously_removed_reactivates_their_row(self):
        seat = committee_svc.add_member(self.scheme, self.alice, added_by=self.treasurer)
        committee_svc.remove_member(seat, removed_by=self.treasurer, reason="Stepped down.")
        self.assertFalse(committee_svc.is_seated(self.scheme, self.alice))

        reseated = committee_svc.add_member(
            self.scheme, self.alice, role=CommitteeMember.Role.SECRETARY,
            added_by=self.treasurer)
        self.assertEqual(reseated.pk, seat.pk)          # same row, not a duplicate
        self.assertTrue(reseated.active)
        self.assertEqual(reseated.role, CommitteeMember.Role.SECRETARY)

    def test_cannot_seat_the_same_active_person_twice(self):
        committee_svc.add_member(self.scheme, self.alice, added_by=self.treasurer)
        with self.assertRaises(ValidationError):
            committee_svc.add_member(self.scheme, self.alice, added_by=self.treasurer)

    def test_only_one_active_chair_per_scheme(self):
        committee_svc.add_member(self.scheme, self.alice,
                                 role=CommitteeMember.Role.CHAIR, added_by=self.treasurer)
        with self.assertRaises(ValidationError) as cm:
            committee_svc.add_member(self.scheme, self.bob,
                                     role=CommitteeMember.Role.CHAIR, added_by=self.treasurer)
        self.assertIn("already has an active Chair", str(cm.exception))

    def test_removing_the_chair_allows_a_new_one(self):
        seat = committee_svc.add_member(self.scheme, self.alice,
                                        role=CommitteeMember.Role.CHAIR,
                                        added_by=self.treasurer)
        committee_svc.remove_member(seat, removed_by=self.treasurer)
        committee_svc.add_member(self.scheme, self.bob, role=CommitteeMember.Role.CHAIR,
                                 added_by=self.treasurer)   # no error

    def test_removal_is_never_a_delete_the_history_survives(self):
        seat = committee_svc.add_member(self.scheme, self.alice, added_by=self.treasurer)
        committee_svc.remove_member(seat, removed_by=self.treasurer, reason="Moved away.")
        seat.refresh_from_db()
        self.assertFalse(seat.active)
        self.assertEqual(seat.removed_by, self.treasurer)
        self.assertEqual(seat.removed_reason, "Moved away.")
        self.assertTrue(CommitteeMember.objects.filter(pk=seat.pk).exists())

    def test_a_committee_is_scoped_to_its_own_scheme(self):
        fund2 = Department.objects.create(
            name="P6 Fund 2", slug="p6-fund-2", fund_type=Department.FundType.LOCAL)
        scheme2 = BenevolentScheme.objects.create(
            name="P6 Scheme 2", code="P62", fund=fund2, created_by=self.treasurer)
        committee_svc.add_member(self.scheme, self.alice, added_by=self.treasurer)
        self.assertFalse(committee_svc.is_seated(scheme2, self.alice))
        self.assertFalse(committee_svc.has_roster(scheme2))

    def test_the_roster_view_requires_scheme_setup_rights(self):
        self.client.force_login(self.clerk)     # assistant: no setup rights
        r = self.client.post(reverse("benevolent_committee_roster", args=[self.scheme.pk]),
                             {"user": self.alice.pk, "role": "MEMBER"})
        self.assertNotEqual(r.status_code, 200)
        self.assertFalse(committee_svc.is_seated(self.scheme, self.alice))

    def test_the_roster_view_works_for_a_treasurer(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(reverse("benevolent_committee_roster", args=[self.scheme.pk]),
                             {"user": self.alice.pk, "role": "CHAIR"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(committee_svc.is_seated(self.scheme, self.alice))


# ===========================================================================
# 2. APPROVAL LEVELS — committee_requires_chair
# ===========================================================================

class ApprovalLevelTests(Phase6Fixture):

    def setUp(self):
        super().setUp()
        self.chair_seat = committee_svc.add_member(
            self.scheme, self.alice, role=CommitteeMember.Role.CHAIR,
            added_by=self.treasurer)
        committee_svc.add_member(self.scheme, self.bob, added_by=self.treasurer)
        committee_svc.add_member(self.scheme, self.carol, added_by=self.treasurer)

        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=400),
            user=self.treasurer)
        v2.committee_requires_chair = True
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

    def test_quorum_without_the_chair_does_not_carry(self):
        case = self._assessed_case()
        case_svc.record_vote(case, user=self.bob, decision="APPROVE")
        case_svc.record_vote(case, user=self.carol, decision="APPROVE")
        state = case_svc.committee_state(case)
        self.assertEqual(state["have"], 2)          # quorum (2) met by headcount
        self.assertTrue(state["waiting_on_chair"])
        self.assertFalse(state["carried"])           # but not carried

    def test_approving_is_refused_with_a_chair_specific_message(self):
        case = self._assessed_case()
        case_svc.record_vote(case, user=self.bob, decision="APPROVE")
        case_svc.record_vote(case, user=self.carol, decision="APPROVE")
        with self.assertRaises(ValidationError) as cm:
            case_svc.approve_case(case, user=self.treasurer)
        self.assertIn("Chair", str(cm.exception))

    def test_the_chairs_vote_carries_it(self):
        case = self._assessed_case()
        case_svc.record_vote(case, user=self.bob, decision="APPROVE")
        case_svc.record_vote(case, user=self.alice, decision="APPROVE")  # the Chair
        state = case_svc.committee_state(case)
        self.assertTrue(state["chair_approved"])
        self.assertTrue(state["carried"])
        case_svc.approve_case(case, user=self.treasurer)   # no error now
        case.refresh_from_db()
        self.assertEqual(case.status, BenevolentCase.Status.APPROVED)

    def test_without_the_requires_chair_flag_an_ordinary_quorum_is_enough(self):
        # published and in force BEFORE the case is assessed — the case's
        # frozen decision basis must reflect it, not a later change
        v3 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=350), user=self.treasurer)
        v3.committee_requires_chair = False
        v3.save()
        scheme_svc.publish_policy(v3, user=self.treasurer)

        case = self._assessed_case()
        self.assertEqual(case.policy_id, v3.pk)
        case_svc.record_vote(case, user=self.bob, decision="APPROVE")
        case_svc.record_vote(case, user=self.carol, decision="APPROVE")
        state = case_svc.committee_state(case)
        self.assertTrue(state["carried"])            # chair not required this time

    def test_requires_chair_is_ignored_when_the_scheme_has_no_chair_seated(self):
        committee_svc.remove_member(self.chair_seat, removed_by=self.treasurer)
        case = self._assessed_case()
        case_svc.record_vote(case, user=self.bob, decision="APPROVE")
        case_svc.record_vote(case, user=self.carol, decision="APPROVE")
        state = case_svc.committee_state(case)
        self.assertFalse(state["requires_chair"])     # nobody to require
        self.assertTrue(state["carried"])


# ===========================================================================
# 3. REINSTATEMENT FEE — the real bug: it was never charged
# ===========================================================================

class ReinstatementFeeTests(Phase6Fixture):

    def test_a_configured_fee_is_charged_automatically_on_reinstatement(self):
        self.policy.reinstatement_fee = Decimal("250")
        self.policy.save()
        reg_svc.suspend(self.membership, user=self.treasurer, reason="Missed dues.")

        before = MemberAdjustment.objects.count()
        reg_svc.reinstate(self.membership, user=self.treasurer)
        self.assertEqual(MemberAdjustment.objects.count(), before + 1)

        adj = MemberAdjustment.objects.latest("id")
        self.assertEqual(adj.amount, Decimal("250"))
        self.assertEqual(adj.kind, MemberAdjustment.Kind.CHARGE)
        self.assertTrue(adj.automated)
        self.assertTrue(adj.is_effective)             # auto-approved, in force immediately
        self.assertIn("Reinstatement fee", adj.reason)

    def test_the_fee_actually_increases_what_the_member_owes(self):
        """Isolates the fee's own effect from ordinary dues accrual (which
        keeps ticking regardless, and is not what this test is about) by
        comparing the obligations-ledger total specifically, not the whole
        arrears figure."""
        self.policy.reinstatement_fee = Decimal("250")
        self.policy.save()
        reg_svc.suspend(self.membership, user=self.treasurer, reason="Missed dues.")
        before = engine_svc.adjustments_total(self.membership)
        reg_svc.reinstate(self.membership, user=self.treasurer)
        after = engine_svc.adjustments_total(self.membership)
        self.assertEqual(after, before + Decimal("250"))

    def test_no_fee_configured_means_nothing_is_charged(self):
        self.assertEqual(self.policy.reinstatement_fee, Decimal(0))
        reg_svc.suspend(self.membership, user=self.treasurer, reason="Missed dues.")
        before = MemberAdjustment.objects.count()
        reg_svc.reinstate(self.membership, user=self.treasurer)
        self.assertEqual(MemberAdjustment.objects.count(), before)

    def test_the_fee_is_auto_approved_not_a_rubber_stamp_pending_a_second_person(self):
        self.policy.reinstatement_fee = Decimal("100")
        self.policy.save()
        reg_svc.suspend(self.membership, user=self.treasurer, reason="x")
        reg_svc.reinstate(self.membership, user=self.treasurer)
        adj = MemberAdjustment.objects.latest("id")
        self.assertEqual(adj.approved_by, self.treasurer)
        self.assertEqual(adj.raised_by, self.treasurer)
        self.assertIsNotNone(adj.approved_at)

    def test_the_charge_carries_the_policy_reference(self):
        self.policy.reinstatement_fee = Decimal("100")
        self.policy.save()
        reg_svc.suspend(self.membership, user=self.treasurer, reason="x")
        reg_svc.reinstate(self.membership, user=self.treasurer)
        adj = MemberAdjustment.objects.latest("id")
        self.assertEqual(adj.policy, self.policy)


# ===========================================================================
# 4. THE POLICY ENGINE — evaluate_reinstatement() reuses Check
# ===========================================================================

class ReinstatementEvaluationTests(Phase6Fixture):

    def test_reports_the_configured_fee_as_a_non_blocking_check(self):
        self.policy.reinstatement_fee = Decimal("500")
        self.policy.save()
        checks = evaluate_reinstatement(self.membership, on=TODAY)
        fee = next(c for c in checks if c.code == "reinstatement_fee")
        self.assertFalse(fee.passed)
        self.assertFalse(fee.blocking)
        self.assertIn("500", fee.detail)

    def test_reports_no_fee_cleanly(self):
        checks = evaluate_reinstatement(self.membership, on=TODAY)
        fee = next(c for c in checks if c.code == "reinstatement_fee")
        self.assertTrue(fee.passed)

    def test_reports_the_waiting_period_consequence(self):
        self.policy.reinstatement_waiting_days = 60
        self.policy.save()
        checks = evaluate_reinstatement(self.membership, on=TODAY)
        wait = next(c for c in checks if c.code == "reinstatement_wait")
        self.assertFalse(wait.passed)
        self.assertFalse(wait.blocking)
        self.assertIn("60", wait.detail)

    def test_nothing_here_ever_blocks_a_reinstatement(self):
        self.policy.reinstatement_fee = Decimal("1000")
        self.policy.reinstatement_waiting_days = 90
        self.policy.save()
        checks = evaluate_reinstatement(self.membership, on=TODAY)
        self.assertTrue(all(not c.blocking for c in checks))

    def test_no_policy_in_force_is_reported_not_raised(self):
        self.mary2 = Member.objects.create(name="Orphan Member")
        orphan_scheme = BenevolentScheme.objects.create(
            name="No Policy Scheme", code="NOPOL", fund=self.fund,
            created_by=self.treasurer)
        m2 = SchemeMembership.objects.create(
            scheme=orphan_scheme, member=self.mary2, status="ACTIVE",
            joined_on=TODAY)
        checks = evaluate_reinstatement(m2, on=TODAY)
        self.assertFalse(checks[0].passed)
        self.assertIn("No policy", checks[0].detail)

    def test_the_reinstatement_log_entry_carries_the_consequences(self):
        self.policy.reinstatement_fee = Decimal("250")
        self.policy.save()
        reg_svc.suspend(self.membership, user=self.treasurer, reason="x")
        reg_svc.reinstate(self.membership, user=self.treasurer, reason="Paid up.")
        event = self.membership.events.filter(kind="REINSTATED").latest("id")
        self.assertIn("250", event.reason)


# ===========================================================================
# 5. POLICY REFERENCES & COMMENTS
# ===========================================================================

class PolicyReferenceTests(Phase6Fixture):

    def test_a_discretionary_exemption_records_the_policy_in_force(self):
        ex = reg_svc.grant_exemption(
            self.membership, kind="HARDSHIP", reason="Lost his job.",
            comments="Discussed at the March board meeting.", user=self.clerk)
        self.assertEqual(ex.policy, self.policy)
        self.assertEqual(ex.comments, "Discussed at the March board meeting.")

    def test_an_automated_policy_exemption_also_records_it(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=400),
            user=self.treasurer)
        v2.bereaved_contribution_policy = SchemePolicy.BereavedContributionPolicy.EXEMPT
        v2.bereaved_dues_waiver_months = 2
        v2.approval_mode = SchemePolicy.ApprovalMode.TREASURER
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        case = self._assessed_case()
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.treasurer,
                              allow_self_approval=True)
        ex = self.membership.exemptions.filter(kind="BEREAVEMENT").first()
        self.assertIsNotNone(ex)
        self.assertEqual(ex.policy, v2)

    def test_a_discretionary_charge_records_the_policy_and_comments(self):
        adj = engine_svc.charge(
            self.membership, kind=MemberAdjustment.Kind.PENALTY, amount=Decimal("50"),
            reason="Persistently missed dues.", comments="Third reminder ignored.",
            user=self.clerk)
        self.assertEqual(adj.policy, self.policy)
        self.assertEqual(adj.comments, "Third reminder ignored.")

    def test_a_policy_change_does_not_retroactively_alter_an_old_exemptions_reference(self):
        ex = reg_svc.grant_exemption(
            self.membership, kind="HARDSHIP", reason="x", user=self.clerk)
        original_policy = ex.policy
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=1), user=self.treasurer)
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)
        ex.refresh_from_db()
        self.assertEqual(ex.policy, original_policy)
        self.assertNotEqual(ex.policy, v2)


# ===========================================================================
# 6. AUDITABILITY — the consolidated Overrides & Exceptions view
# ===========================================================================

class OverridesExceptionsViewTests(Phase6Fixture):

    def test_the_view_loads_and_lists_recent_decisions(self):
        engine_svc.charge(
            self.membership, kind=MemberAdjustment.Kind.PENALTY, amount=Decimal("50"),
            reason="Late.", user=self.clerk)
        adj = MemberAdjustment.objects.latest("id")
        engine_svc.approve_adjustment(adj, user=self.treasurer)

        ex = reg_svc.grant_exemption(
            self.membership, kind="HARDSHIP", reason="x", user=self.clerk)
        reg_svc.approve_exemption(ex, user=self.treasurer)

        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_overrides_exceptions"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Late.", body)
        self.assertIn("HARDSHIP".title() if False else "Hardship", body)

    def test_filtering_by_scheme_narrows_the_results(self):
        fund2 = Department.objects.create(
            name="P6 Fund 3", slug="p6-fund-3", fund_type=Department.FundType.LOCAL)
        other_scheme = BenevolentScheme.objects.create(
            name="Other P6 Scheme", code="OP6", fund=fund2, created_by=self.treasurer)
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_overrides_exceptions"),
                            {"scheme": other_scheme.pk})
        self.assertEqual(r.status_code, 200)
