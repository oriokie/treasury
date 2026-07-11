"""Phase 2 — Constitution, Settings & Policy Engine.

The tests are grouped around the claims Phase 2 actually makes:

  1. SETTINGS vs POLICY   the line that the whole phase rests on. A setting can
                          be changed freely and cannot rewrite history; a rule is
                          versioned and frozen once used.
  2. THE CONSTITUTION     every new rule — registration, renewals, hybrid dues,
                          committee approval, bereaved exemptions, inactivity,
                          household cover, inheritance — actually decides cases,
                          from configuration, with no code branch per scheme.
  3. PROFILES & WIZARD    a church can configure a scheme by answering questions
                          rather than filling in 54 fields, and can check the
                          wizard's work.
  4. AUTOMATION           applies the policy's rules without ever overriding a
                          human, and reverses itself when the facts change.
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
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                               BenevolentSettings, CaseApproval, PolicyProfile,
                               SchemeBenefitRule, SchemeDependant, SchemeMembership,
                               SchemeNominee, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import profiles as profile_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import wizard as wizard_svc
from benevolent.services.eligibility import evaluate

TODAY = dt.date.today()


def _months_ago(n):
    y, m = TODAY.year, TODAY.month - n
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, 1)


class Phase2Fixture(TestCase):
    """A scheme whose policy exercises the Phase 2 rules."""

    def setUp(self):
        self.treasurer = User.objects.create_user("t2", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c2", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

        self.fund = Department.objects.create(
            name="Welfare Fund", slug="welfare-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Welfare Scheme", code="WEL", fund=self.fund,
            created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")

        self.policy = self._policy()
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.mary = Member.objects.create(name="Mary W", phone="254700000001")
        self.john = Member.objects.create(name="John O", phone="254700000002")

    def _policy(self, **kw):
        defaults = dict(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=400),
            membership_required=True, waiting_period_days=30,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("10000"),
            created_by=self.treasurer)
        defaults.update(kw)
        return SchemePolicy.objects.create(**defaults)

    def _new_version(self, effective_from=None, **kw):
        """Supersede the live policy with a new version carrying these rules."""
        v = scheme_svc.new_version_from(
            self.policy,
            effective_from=effective_from or (TODAY - dt.timedelta(days=200)),
            user=self.treasurer)
        for k, val in kw.items():
            setattr(v, k, val)
        v.save()
        scheme_svc.publish_policy(v, user=self.treasurer)
        return v

    def _enrol(self, member, days_ago=200, **kw):
        return scheme_svc.enrol(self.scheme, member,
                                joined_on=TODAY - dt.timedelta(days=days_ago),
                                user=self.treasurer, **kw)

    def _case(self, membership, event_date=None, raised_by=None):
        return BenevolentCase.objects.create(
            scheme=self.scheme, membership=membership, event_type=self.bereavement,
            event_date=event_date or (TODAY - dt.timedelta(days=2)),
            reported_date=TODAY, raised_by=raised_by or self.clerk)

    def _assess(self, case):
        """Submit then assess — the workflow never lets a draft be assessed."""
        if case.status == BenevolentCase.Status.DRAFT:
            case_svc.submit_case(case, user=self.clerk)
        return case_svc.assess_case(case, user=self.treasurer)


# ===========================================================================
# 1. SETTINGS vs POLICY — the line the whole phase rests on
# ===========================================================================

class SettingsVersusPolicyTests(Phase2Fixture):

    def test_settings_are_a_singleton_and_freely_editable(self):
        cfg = BenevolentSettings.get()
        cfg.automation_enabled = True
        cfg.save()
        # no versioning, no lock, no ceremony — and still exactly one row
        cfg2 = BenevolentSettings.get()
        self.assertTrue(cfg2.automation_enabled)
        self.assertEqual(BenevolentSettings.objects.count(), 1)

    def test_a_rule_is_locked_once_used_but_a_setting_never_is(self):
        """The distinction in one test. A used policy refuses to change; the
        settings row next to it changes freely, because nothing on it could
        rewrite what was decided."""
        m = self._enrol(self.mary)
        case = self._case(m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)

        self.policy.refresh_from_db()
        self.policy.benefit_amount = Decimal("999999")
        with self.assertRaises(ValidationError):
            self.policy.save()

        cfg = BenevolentSettings.get()
        cfg.default_benefit_category = "OTHER"
        cfg.notify_on_case_approved = False
        cfg.save()                       # no complaint — this is not a rule
        self.assertEqual(BenevolentSettings.get().default_benefit_category, "OTHER")

    def test_changing_an_accounting_mapping_cannot_rewrite_a_posted_document(self):
        """The claim that lets accounting mappings live in settings at all: a
        posted document carries its own fund, so re-pointing the mapping steers
        only what comes next."""
        other = Department.objects.create(name="Fees Fund", slug="fees-fund",
                                          fund_type=Department.FundType.LOCAL)
        m = self._enrol(self.mary)
        c1 = contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"),
            membership=m, user=self.treasurer)
        self.assertEqual(c1.transaction.department, self.fund)

        cfg = BenevolentSettings.get()
        cfg.registration_fee_fund = other
        cfg.save()

        c1.refresh_from_db()
        self.assertEqual(c1.transaction.department, self.fund)   # untouched

        v2 = self._new_version(registration_fee=Decimal("500"),
                               registration_required=True)
        fee = contrib_svc.record_fee(m, kind="REGISTRATION", user=self.treasurer)
        self.assertEqual(fee.transaction.department, other)       # the NEW one only

    def test_the_rule_fields_list_is_what_gets_frozen(self):
        """Every constitution dimension must actually be under the version lock —
        a rule that is not in RULE_FIELDS is a rule that could be changed after a
        case was decided on it."""
        for f in ["registration_required", "renewal_required", "levy_amount",
                  "approval_mode", "committee_quorum", "bereaved_exempt_own_levy",
                  "inactivity_action", "household_mode", "inheritance_mode",
                  "arrears_treatment", "funding_methods"]:
            self.assertIn(f, SchemePolicy.RULE_FIELDS, f)
        snap = self.policy.terms_snapshot()
        for f in SchemePolicy.RULE_FIELDS:
            self.assertIn(f, snap, f)


# ===========================================================================
# 2. THE CONSTITUTION — every rule decides cases, from configuration
# ===========================================================================

class RegistrationTests(Phase2Fixture):

    def test_registration_approval_holds_a_member_pending(self):
        self._new_version(
            registration_required=True,
            registration_approval=SchemePolicy.RegistrationApproval.TREASURER)
        m = self._enrol(self.mary, days_ago=100)
        self.assertEqual(m.status, SchemeMembership.Status.PENDING)

        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertFalse(r.eligible)
        codes = [c.code for c in r.blocking_failures]
        self.assertIn("registration", codes)

    def test_cover_runs_from_ADMISSION_not_from_enrolment(self):
        """A member typed into a list in January but admitted in June is covered
        from June — otherwise the waiting period is served by paperwork sitting in
        a drawer."""
        self._new_version(
            registration_required=True, waiting_period_days=30,
            registration_approval=SchemePolicy.RegistrationApproval.TREASURER)
        m = self._enrol(self.mary, days_ago=100)
        scheme_svc.admit(m, on=TODAY - dt.timedelta(days=10), user=self.treasurer)
        m.refresh_from_db()
        self.assertEqual(m.cover_from, TODAY - dt.timedelta(days=10))

        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        wait = next(c for c in r.checks if c.code == "waiting_period")
        self.assertFalse(wait.passed)          # 10 days served, 30 needed
        self.assertIn("from registration", wait.detail)

    def test_an_unpaid_registration_fee_blocks_a_claim(self):
        self._new_version(registration_required=True,
                          registration_fee=Decimal("500"),
                          waiting_period_days=0)
        m = self._enrol(self.mary, days_ago=100)
        # registration is AUTO here, so they are admitted at once — it is the
        # unpaid FEE alone that stands between them and a claim
        self.assertEqual(m.status, SchemeMembership.Status.ACTIVE)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertIn("registration", [c.code for c in r.blocking_failures])

        contrib_svc.record_fee(m, kind="REGISTRATION", user=self.treasurer)
        m.refresh_from_db()
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertTrue(r.eligible)

    def test_a_registration_fee_is_not_a_due(self):
        """It must not pay off the member's monthly contributions — a fee and a
        due are different money."""
        self._new_version(registration_required=True, registration_fee=Decimal("500"))
        m = self._enrol(self.mary, days_ago=60)
        before = contrib_svc.arrears_for(m)
        contrib_svc.record_fee(m, kind="REGISTRATION", user=self.treasurer)
        after = contrib_svc.arrears_for(m)
        self.assertEqual(before, after)

    def test_joining_age_is_measured_at_joining_not_today(self):
        self._new_version(max_age=70, waiting_period_days=0)
        old_dob = dt.date(TODAY.year - 71, 6, 1)
        m = self._enrol(self.mary, days_ago=1000, date_of_birth=old_dob)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        age_check = next(c for c in r.checks if c.code == "joining_age")
        # they were 68 when they joined ~3 years ago, so they are IN, even though
        # they are 71 now
        self.assertTrue(age_check.passed, age_check.detail)


class RenewalTests(Phase2Fixture):

    def test_an_overdue_renewal_blocks_a_claim(self):
        self._new_version(
            renewal_required=True,
            renewal_period=SchemePolicy.RenewalPeriod.ANNUAL,
            renewal_month=1, renewal_grace_days=30, lapse_on_non_renewal=True,
            waiting_period_days=0)
        m = self._enrol(self.mary, days_ago=800)
        m.renewed_until = TODAY - dt.timedelta(days=90)
        m.save()
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertFalse(r.eligible)
        self.assertIn("renewal", [c.code for c in r.blocking_failures])

    def test_paying_the_renewal_moves_the_subscription_on(self):
        self._new_version(
            renewal_required=True,
            renewal_period=SchemePolicy.RenewalPeriod.ANNUAL,
            renewal_fee=Decimal("300"), renewal_month=1, waiting_period_days=0)
        m = self._enrol(self.mary, days_ago=800)
        m.renewed_until = TODAY - dt.timedelta(days=90)
        m.save()
        contrib_svc.record_fee(m, kind="RENEWAL", user=self.treasurer)
        m.refresh_from_db()
        self.assertGreater(m.renewed_until, TODAY)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertTrue(r.eligible)

    def test_a_policy_that_does_not_lapse_only_WARNS_about_a_late_renewal(self):
        self._new_version(
            renewal_required=True,
            renewal_period=SchemePolicy.RenewalPeriod.ANNUAL,
            lapse_on_non_renewal=False, waiting_period_days=0)
        m = self._enrol(self.mary, days_ago=800)
        m.renewed_until = TODAY - dt.timedelta(days=90)
        m.save()
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertTrue(r.eligible)                     # not refused …
        self.assertIn("renewal", [c.code for c in r.warnings])   # … but seen


class ContributionModelTests(Phase2Fixture):

    def test_hybrid_charges_dues_AND_a_levy(self):
        self._new_version(
            contribution_mode=SchemePolicy.ContributionMode.HYBRID,
            contribution_amount=Decimal("100"), levy_amount=Decimal("500"),
            waiting_period_days=0)
        m = self._enrol(self.mary, days_ago=90)
        self.john_m = self._enrol(self.john, days_ago=90)

        # dues accrue …
        self.assertGreater(contrib_svc.arrears_for(m), Decimal(0))

        # … and a levy can be raised on a case, on top
        case = self._case(m)
        levy = contrib_svc.raise_case_levy(case)
        self.assertEqual(levy["per_member"], Decimal("500"))

    def test_a_levy_payment_never_clears_a_members_dues_arrears(self):
        """Two different pots of money. If a levy could pay off dues, a member
        chipping in for someone else's bereavement would appear to have paid their
        own subscription, and the scheme's arrears book would quietly go wrong."""
        self._new_version(
            contribution_mode=SchemePolicy.ContributionMode.HYBRID,
            contribution_amount=Decimal("100"), levy_amount=Decimal("500"),
            waiting_period_days=0)
        mary = self._enrol(self.mary, days_ago=90)
        john = self._enrol(self.john, days_ago=90)
        case = self._case(mary)

        owed_before = contrib_svc.arrears_for(john)
        self.assertGreater(owed_before, Decimal(0))

        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), membership=john,
            case=case, user=self.treasurer)

        self.assertEqual(contrib_svc.arrears_for(john), owed_before)   # unmoved
        self.assertEqual(contrib_svc.levy_collected(case), Decimal("500"))

    def test_the_bereaved_member_is_not_levied_for_their_own_case(self):
        self._new_version(
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"), bereaved_exempt_own_levy=True,
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            benefit_mode=SchemePolicy.BenefitMode.POOLED,
            waiting_period_days=0)
        mary = self._enrol(self.mary, days_ago=90)
        self._enrol(self.john, days_ago=90)
        case = self._case(mary)

        levy = contrib_svc.raise_case_levy(case)
        levied = [r["membership"].pk for r in levy["rows"]]
        self.assertNotIn(mary.pk, levied)
        self.assertIn(mary, levy["exempt"])
        self.assertEqual(len(levy["rows"]), 1)          # John alone


class BenefitCalculationTests(Phase2Fixture):

    def test_pooled_benefit_is_what_the_levy_actually_collected(self):
        self._new_version(
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            benefit_mode=SchemePolicy.BenefitMode.POOLED,
            benefit_rounding=SchemePolicy.Rounding.NONE,
            waiting_period_days=0)
        mary = self._enrol(self.mary, days_ago=90)
        john = self._enrol(self.john, days_ago=90)
        case = self._case(mary)

        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), membership=john,
            case=case, user=self.treasurer)

        result = self._assess(case)
        self.assertEqual(result.entitlement.amount, Decimal("500.00"))
        self.assertIn("Levy collected", result.entitlement.workings[0])

    def test_per_member_multiple_is_the_pledge_not_the_reality(self):
        """Deliberately distinct from POOLED: this is what the scheme PROMISES if
        everybody pays, and it must not silently become what was collected."""
        self._new_version(
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            benefit_mode=SchemePolicy.BenefitMode.PER_MEMBER_MULTIPLE,
            bereaved_exempt_own_levy=True, benefit_cap=None,
            waiting_period_days=0)
        mary = self._enrol(self.mary, days_ago=90)
        self._enrol(self.john, days_ago=90)
        case = self._case(mary)
        # NOBODY has paid — and the promised benefit is still 500 × 1 other member
        result = self._assess(case)
        self.assertEqual(result.entitlement.amount, Decimal("500.00"))
        self.assertEqual(contrib_svc.levy_collected(case), Decimal(0))

    def test_arrears_are_DEDUCTED_not_used_to_refuse_a_bereaved_family(self):
        self._new_version(
            arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("10000"), waiting_period_days=0)
        m = self._enrol(self.mary, days_ago=95)      # ~3-4 months of dues owed
        owed = contrib_svc.arrears_for(m)
        self.assertGreater(owed, Decimal(0))

        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertTrue(r.eligible)                  # NOT refused
        self.assertEqual(r.entitlement.amount, Decimal("10000") - owed)
        self.assertTrue(any("Arrears" in d for d in r.entitlement.deductions))

    def test_rounding_applies_after_the_cap(self):
        self._new_version(
            benefit_mode=SchemePolicy.BenefitMode.PERCENTAGE,
            benefit_percent=Decimal("60"), benefit_cap=Decimal("23847"),
            benefit_rounding=SchemePolicy.Rounding.HUNDRED,
            membership_required=False, waiting_period_days=0,
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     claimed_amount=Decimal("100000"))
        # 60,000 capped to 23,847, then rounded to 23,800
        self.assertEqual(r.entitlement.amount, Decimal("23800.00"))

    def test_deducting_the_own_levy_and_exempting_it_are_mutually_exclusive(self):
        from benevolent.forms import PolicyForm
        form = PolicyForm(data={
            "effective_from": TODAY.isoformat(),
            "contribution_mode": "PER_CASE_LEVY", "levy_amount": "500",
            "benefit_mode": "FIXED", "benefit_amount": "1000",
            "bereaved_exempt_own_levy": "on", "bereaved_deduct_own_levy": "on",
            "approval_mode": "TREASURER", "committee_quorum": "3",
            "arrears_treatment": "IGNORE", "registration_approval": "AUTO",
            "renewal_period": "NONE", "inactivity_action": "NONE",
            "household_mode": "INDIVIDUAL", "inheritance_mode": "NONE",
            "benefit_rounding": "NONE", "contribution_frequency": "MONTHLY",
            "renewal_month": "1",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("bereaved_deduct_own_levy", form.errors)

    def test_a_pooled_benefit_without_a_levy_is_rejected_at_the_form(self):
        """It would silently compute zero for every case, forever."""
        from benevolent.forms import PolicyForm
        form = PolicyForm(data={
            "effective_from": TODAY.isoformat(),
            "contribution_mode": "FIXED_PERIODIC", "contribution_amount": "100",
            "benefit_mode": "POOLED",
            "approval_mode": "TREASURER", "committee_quorum": "3",
            "arrears_treatment": "IGNORE", "registration_approval": "AUTO",
            "renewal_period": "NONE", "inactivity_action": "NONE",
            "household_mode": "INDIVIDUAL", "inheritance_mode": "NONE",
            "benefit_rounding": "NONE", "contribution_frequency": "MONTHLY",
            "renewal_month": "1",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("benefit_mode", form.errors)


class CommitteeApprovalTests(Phase2Fixture):

    def setUp(self):
        super().setUp()
        self._new_version(
            approval_mode=SchemePolicy.ApprovalMode.COMMITTEE,
            committee_quorum=3, waiting_period_days=0)
        self.m = self._enrol(self.mary, days_ago=90)
        self.case = self._case(self.m)
        case_svc.submit_case(self.case, user=self.clerk)
        case_svc.assess_case(self.case, user=self.treasurer)
        self.elders = []
        for i in range(4):
            u = User.objects.create_user(f"elder{i}", password="x")
            u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
            self.elders.append(u)

    def test_a_treasurer_cannot_approve_past_the_committee(self):
        """The whole point of a committee: it is not one person, and no individual
        — however senior — can stand in for it."""
        with self.assertRaises(ValidationError) as cm:
            case_svc.approve_case(self.case, amount=Decimal("10000"),
                                  user=self.treasurer)
        self.assertIn("cannot be authorised by one person", str(cm.exception))

    def test_a_quorum_carries_the_case(self):
        for u in self.elders[:3]:
            case_svc.record_vote(self.case, user=u,
                                 decision=CaseApproval.Decision.APPROVE)
        state = case_svc.committee_state(self.case)
        self.assertTrue(state["carried"])
        case_svc.approve_case(self.case, amount=Decimal("10000"), user=self.treasurer)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, BenevolentCase.Status.APPROVED)

    def test_the_quorum_agrees_on_the_LOWEST_amount_every_member_would_allow(self):
        """Three people voting 10,000 / 8,000 / 10,000 have not agreed on 10,000.
        They have agreed on 8,000 — that is the most all three sanctioned."""
        case_svc.record_vote(self.case, user=self.elders[0],
                             decision=CaseApproval.Decision.APPROVE,
                             amount=Decimal("10000"))
        case_svc.record_vote(self.case, user=self.elders[1],
                             decision=CaseApproval.Decision.APPROVE,
                             amount=Decimal("8000"))
        case_svc.record_vote(self.case, user=self.elders[2],
                             decision=CaseApproval.Decision.APPROVE,
                             amount=Decimal("10000"))
        self.assertEqual(case_svc.committee_state(self.case)["agreed_amount"],
                         Decimal("8000"))
        with self.assertRaises(ValidationError):
            case_svc.approve_case(self.case, amount=Decimal("10000"),
                                  user=self.treasurer)
        case_svc.approve_case(self.case, amount=Decimal("8000"), user=self.treasurer)
        self.case.refresh_from_db()
        self.assertEqual(self.case.approved_amount, Decimal("8000"))

    def test_one_member_votes_once_and_may_change_their_mind(self):
        case_svc.record_vote(self.case, user=self.elders[0],
                             decision=CaseApproval.Decision.REJECT)
        case_svc.record_vote(self.case, user=self.elders[0],
                             decision=CaseApproval.Decision.APPROVE)
        self.assertEqual(self.case.committee_approvals.count(), 1)
        self.assertEqual(case_svc.committee_state(self.case)["have"], 1)

    def test_two_stage_sends_only_the_big_ones_to_the_committee(self):
        self._new_version(
            effective_from=TODAY - dt.timedelta(days=150),
            approval_mode=SchemePolicy.ApprovalMode.TWO_STAGE,
            committee_threshold=Decimal("20000"), committee_quorum=3,
            benefit_mode=SchemePolicy.BenefitMode.DISCRETIONARY,
            benefit_cap=Decimal("100000"), waiting_period_days=0)
        small = self._case(self.m)
        small.claimed_amount = Decimal("5000")
        small.save()
        case_svc.submit_case(small, user=self.clerk)
        case_svc.assess_case(small, user=self.treasurer)
        self.assertEqual(case_svc.approval_route(small), "TREASURER")
        case_svc.approve_case(small, amount=Decimal("5000"), user=self.treasurer)

        big = self._case(self.m, event_date=TODAY - dt.timedelta(days=1))
        big.claimed_amount = Decimal("50000")
        big.save()
        case_svc.submit_case(big, user=self.clerk)
        case_svc.assess_case(big, user=self.treasurer)
        self.assertEqual(case_svc.approval_route(big, amount=Decimal("50000")),
                         "COMMITTEE")
        with self.assertRaises(ValidationError):
            case_svc.approve_case(big, amount=Decimal("50000"), user=self.treasurer)


class HouseholdAndInheritanceTests(Phase2Fixture):

    def test_a_child_over_the_age_limit_is_not_covered(self):
        self._new_version(dependant_age_limit=18, waiting_period_days=0)
        m = self._enrol(self.mary, days_ago=200)
        self.bereavement.covers_dependants = True
        self.bereavement.save()
        grown = SchemeDependant.objects.create(
            membership=m, name="Adult child", relationship=SchemeDependant.Relationship.CHILD,
            date_of_birth=dt.date(TODAY.year - 25, 1, 1),
            registered_on=TODAY - dt.timedelta(days=150))
        case = self._case(m)
        case.dependant = grown
        case.save()
        r = self._assess(case)
        self.assertFalse(r.eligible)
        self.assertIn("beneficiary", [c.code for c in r.blocking_failures])

    def test_the_dependant_cap_covers_those_registered_first(self):
        self._new_version(max_dependants=2, waiting_period_days=0,
                          spouse_auto_covered=False)
        m = self._enrol(self.mary, days_ago=200)
        self.bereavement.covers_dependants = True
        self.bereavement.save()
        deps = [SchemeDependant.objects.create(
            membership=m, name=f"Child {i}",
            relationship=SchemeDependant.Relationship.CHILD,
            registered_on=TODAY - dt.timedelta(days=180 - i))
            for i in range(3)]
        case = self._case(m)
        case.dependant = deps[2]        # the third registered
        case.save()
        r = self._assess(case)
        self.assertFalse(r.eligible)
        self.assertIn("registered first",
                      next(c.detail for c in r.checks if c.code == "beneficiary"))

    def test_a_nominee_policy_reports_a_missing_nominee_rather_than_guessing(self):
        self._new_version(
            inheritance_mode=SchemePolicy.InheritanceMode.NOMINEE,
            waiting_period_days=0)
        m = self._enrol(self.mary, days_ago=200)
        case = self._case(m)
        r = self._assess(case)
        nom = next(c for c in r.checks if c.code == "nominee")
        self.assertFalse(nom.passed)
        self.assertFalse(nom.blocking)          # flagged, not refused
        self.assertTrue(r.eligible)

        SchemeNominee.objects.create(membership=m, name="Peter W",
                                     relationship="Son", share_percent=Decimal("100"))
        r = case_svc.assess_case(case, user=self.treasurer)   # re-assess
        self.assertTrue(next(c for c in r.checks if c.code == "nominee").passed)

    def test_nominee_shares_cannot_exceed_100_percent(self):
        m = self._enrol(self.mary, days_ago=200)
        SchemeNominee.objects.create(membership=m, name="A",
                                     share_percent=Decimal("60"))
        n = SchemeNominee(membership=m, name="B", share_percent=Decimal("50"))
        with self.assertRaises(ValidationError):
            n.full_clean()


class ReinstatementTests(Phase2Fixture):

    def test_a_returning_member_serves_the_waiting_period_AGAIN(self):
        """The single most obvious way to game a welfare scheme: lapse for years,
        rejoin the week a relative falls ill, and claim on a 2019 joining date."""
        self._new_version(
            waiting_period_days=30, reinstatement_waiting_days=90,
            inactivity_action=SchemePolicy.InactivityAction.LAPSE,
            inactivity_months=6)
        m = self._enrol(self.mary, days_ago=1000)
        m.status = SchemeMembership.Status.LAPSED
        m.save()

        scheme_svc.reinstate(m, on=TODAY - dt.timedelta(days=10),
                             user=self.treasurer)
        m.refresh_from_db()
        self.assertEqual(m.cover_from, TODAY - dt.timedelta(days=10))

        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        wait = next(c for c in r.checks if c.code == "waiting_period")
        self.assertFalse(wait.passed)
        self.assertIn("from reinstatement", wait.detail)
        self.assertIn("90 required", wait.detail)

    def test_re_enrolling_a_former_member_reinstates_rather_than_duplicating(self):
        m = self._enrol(self.mary, days_ago=500)
        original = m.number
        scheme_svc.withdraw_membership(m, user=self.treasurer)
        again = scheme_svc.enrol(self.scheme, self.mary, joined_on=TODAY,
                                 user=self.treasurer)
        self.assertEqual(again.pk, m.pk)
        self.assertEqual(again.number, original)      # history is never orphaned
        self.assertEqual(again.reinstated_on, TODAY)


# ===========================================================================
# 3. PROFILES & THE WIZARD
# ===========================================================================

class ProfileTests(Phase2Fixture):

    def test_the_builtin_library_installs_and_is_idempotent(self):
        n = profile_svc.install_builtins()
        self.assertEqual(n, 4)
        self.assertEqual(profile_svc.install_builtins(), 0)
        self.assertEqual(PolicyProfile.objects.filter(builtin=True).count(), 4)

    def test_a_builtin_profile_cannot_be_deleted(self):
        profile_svc.install_builtins()
        p = PolicyProfile.objects.filter(builtin=True).first()
        with self.assertRaises(ValidationError):
            p.delete()
        # but it can be copied and the copy adjusted freely
        copy = profile_svc.duplicate(p)
        self.assertFalse(copy.builtin)
        copy.delete()

    def test_applying_a_profile_creates_a_DRAFT_never_a_live_policy(self):
        profile_svc.install_builtins()
        p = PolicyProfile.objects.get(name="Monthly dues, fixed benefit")
        draft = profile_svc.apply_profile(p, self.scheme, effective_from=TODAY,
                                          user=self.treasurer)
        self.assertEqual(draft.status, SchemePolicy.Status.DRAFT)
        # the scheme is still governed by the ORIGINAL policy
        self.assertEqual(self.scheme.policy_on(TODAY).pk, self.policy.pk)

    def test_a_profile_brings_its_events_with_it(self):
        profile_svc.install_builtins()
        p = PolicyProfile.objects.get(name="Monthly dues, fixed benefit")
        draft = profile_svc.apply_profile(p, self.scheme, effective_from=TODAY)
        codes = set(self.scheme.event_types.values_list("code", flat=True))
        self.assertTrue({"BER_MEMBER", "BER_SPOUSE", "BER_PARENT"} <= codes)
        self.assertEqual(draft.benefit_rules.count(), 4)

    def test_json_values_are_coerced_to_the_right_python_types(self):
        """A decimal arriving as a string and never converted is how a policy ends
        up comparing '500' to Decimal('500') at assessment time and deciding the
        wrong way, silently."""
        profile_svc.install_builtins()
        p = PolicyProfile.objects.get(name="Monthly dues, fixed benefit")
        draft = profile_svc.apply_profile(p, self.scheme, effective_from=TODAY)
        self.assertIsInstance(draft.contribution_amount, Decimal)
        self.assertIsInstance(draft.waiting_period_days, int)
        self.assertIsInstance(draft.membership_required, bool)
        self.assertEqual(draft.contribution_amount, Decimal("200"))

    def test_policy_to_profile_to_policy_round_trips(self):
        p = profile_svc.save_as_profile(self.policy, name="Ours", user=self.treasurer)
        draft = profile_svc.apply_profile(p, self.scheme, effective_from=TODAY)
        for f in SchemePolicy.RULE_FIELDS:
            if f == "effective_from":
                continue
            self.assertEqual(getattr(draft, f), getattr(self.policy, f), f)


class WizardTests(Phase2Fixture):

    ANSWERS = {
        "purpose": "BENEVOLENT",
        "funding": "HYBRID", "dues_amount": "150", "dues_frequency": "MONTHLY",
        "levy_amount": "400", "arrears": "DEDUCT",
        "registration": "TREASURER", "joining_fee": "500", "waiting_days": "60",
        "renewal": "ANNUAL", "renewal_fee": "200",
        "household": "HOUSEHOLD", "max_dependants": "6", "child_age_limit": "21",
        "benefit": "SCHEDULE", "benefit_cap": "80000", "claim_window": "90",
        "bereaved_levy": "EXEMPT", "dues_waiver": "3",
        "approval": "TWO_STAGE", "committee_threshold": "40000",
        "committee_quorum": "3",
        "inactivity": "LAPSE", "inactivity_months": "12", "rejoin_wait": "90",
        "inheritance": "NOMINEE", "transfer_membership": "yes",
    }

    def test_the_wizard_translates_a_constitution_into_policy_fields(self):
        cfg, lines, why = wizard_svc.build_config(self.ANSWERS)
        self.assertEqual(cfg["contribution_mode"], "HYBRID")
        self.assertEqual(cfg["contribution_amount"], "150")
        self.assertEqual(cfg["levy_amount"], "400")
        self.assertEqual(cfg["waiting_period_days"], 60)
        self.assertEqual(cfg["approval_mode"], "TWO_STAGE")
        self.assertEqual(cfg["committee_threshold"], "40000")
        self.assertEqual(cfg["arrears_treatment"], "DEDUCT")
        self.assertTrue(cfg["bereaved_exempt_own_levy"])
        self.assertEqual(cfg["bereaved_dues_waiver_months"], 3)
        self.assertEqual(cfg["inactivity_action"], "LAPSE")
        self.assertEqual(cfg["reinstatement_waiting_days"], 90)
        self.assertEqual(cfg["inheritance_mode"], "NOMINEE")
        self.assertEqual(sorted(cfg["funding_methods"]), ["DONATION", "DUES", "LEVY"])

    def test_the_wizard_shows_its_reasoning_for_everything_it_sets(self):
        """A wizard that cannot be checked is a wizard that should not be trusted —
        and WILL be."""
        cfg, _lines, why = wizard_svc.build_config(self.ANSWERS)
        explained = {d.setting for d in why}
        for key in cfg:
            self.assertIn(key, explained, f"{key} was set with no reason given")
        self.assertTrue(all(d.because.strip() for d in why))

    def test_every_field_the_wizard_produces_is_a_real_policy_rule(self):
        cfg, _l, _w = wizard_svc.build_config(self.ANSWERS)
        for key in cfg:
            self.assertIn(key, SchemePolicy.RULE_FIELDS, key)

    def test_the_wizard_output_actually_makes_a_working_policy(self):
        """The end-to-end claim: answers in, a policy that decides cases out."""
        cfg, lines, _w = wizard_svc.build_config(self.ANSWERS)
        temp = PolicyProfile(name="(t)", config=cfg, benefit_lines=lines)
        draft = profile_svc.apply_profile(temp, self.scheme,
                                          effective_from=TODAY - dt.timedelta(days=300),
                                          user=self.treasurer)
        draft.full_clean(exclude=["version", "created_by"])
        self.assertEqual(draft.contribution_amount, Decimal("150"))
        self.assertEqual(draft.approval_mode, SchemePolicy.ApprovalMode.TWO_STAGE)

    def test_a_discretionary_benefit_is_always_given_a_cap(self):
        """Without one there is no limit on what a single approval can authorise —
        so the wizard refuses to leave it open, and says so."""
        answers = dict(self.ANSWERS, benefit="DISCRETIONARY", benefit_cap="0")
        cfg, _l, why = wizard_svc.build_config(answers)
        self.assertTrue(Decimal(cfg["benefit_cap"]) > 0)
        reason = next(d.because for d in why if d.setting == "benefit_cap")
        self.assertIn("no limit on what one approval", reason)

    def test_questions_are_hidden_when_they_do_not_apply(self):
        answers = {"funding": "VOLUNTARY"}
        keys = {q.key for q in wizard_svc.visible_questions(answers)}
        self.assertNotIn("dues_amount", keys)
        self.assertNotIn("levy_amount", keys)
        self.assertNotIn("bereaved_levy", keys)

    def test_the_plain_english_summary_reflects_the_answers(self):
        s = wizard_svc.summarise(self.ANSWERS)
        self.assertIn("150", s)
        self.assertIn("400", s)
        self.assertIn("60 days", s)
        self.assertIn("40000", s)


# ===========================================================================
# 4. AUTOMATION
# ===========================================================================

class AutomationTests(Phase2Fixture):

    def test_it_does_nothing_when_switched_off(self):
        result = scheme_svc.run_automation()
        self.assertFalse(result["ran"])
        self.assertIn("switched off", result["reason"])

    def test_it_lapses_a_member_in_arrears_and_reinstates_them_when_they_pay(self):
        self._new_version(arrears_block=True,
                          arrears_treatment=SchemePolicy.ArrearsTreatment.BLOCK,
                          max_arrears_allowed=Decimal("100"))
        m = self._enrol(self.mary, days_ago=200)

        r = scheme_svc.run_automation(self.scheme, force=True)
        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.LAPSED)
        self.assertEqual(r["changed"], 1)

        # they pay everything off
        owed = contrib_svc.arrears_for(m)
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=owed, membership=m,
            user=self.treasurer, period_label="")

        scheme_svc.run_automation(self.scheme, force=True)
        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.ACTIVE)

    def test_it_never_touches_a_status_a_human_set(self):
        """A membership someone deliberately suspended must not be quietly
        reinstated by a nightly job — that is how people stop trusting automation."""
        self._new_version(arrears_block=True,
                          arrears_treatment=SchemePolicy.ArrearsTreatment.BLOCK)
        m = self._enrol(self.mary, days_ago=200)
        m.status = SchemeMembership.Status.SUSPENDED
        m.save()

        scheme_svc.run_automation(self.scheme, force=True)
        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.SUSPENDED)

    def test_it_never_suspends_or_expels_anyone_by_itself(self):
        """Removing someone from a welfare scheme is a decision a person should
        make and answer for. The policy still bars their claims; automation simply
        declines to be the one who throws them out."""
        self._new_version(
            inactivity_months=1,
            inactivity_action=SchemePolicy.InactivityAction.EXPEL,
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE)
        m = self._enrol(self.mary, days_ago=400)

        scheme_svc.run_automation(self.scheme, force=True)
        m.refresh_from_db()
        self.assertNotEqual(m.status, SchemeMembership.Status.EXPELLED)
        self.assertEqual(m.status, SchemeMembership.Status.ACTIVE)

        # but the ENGINE still refuses their claim, which is the part that matters
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertIn("inactivity", [c.code for c in r.blocking_failures])

    def test_it_flags_an_inactive_member_and_unflags_them_when_they_return(self):
        self._new_version(
            inactivity_months=6,
            inactivity_action=SchemePolicy.InactivityAction.FLAG,
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE)
        m = self._enrol(self.mary, days_ago=400)

        scheme_svc.run_automation(self.scheme, force=True)
        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.INACTIVE)
        self.assertEqual(m.inactive_since, TODAY)

        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("100"), membership=m,
            user=self.treasurer)
        scheme_svc.run_automation(self.scheme, force=True)
        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.ACTIVE)
        self.assertIsNone(m.inactive_since)

    def test_it_reports_every_change_it_makes(self):
        self._new_version(arrears_block=True,
                          arrears_treatment=SchemePolicy.ArrearsTreatment.BLOCK)
        self._enrol(self.mary, days_ago=200)
        r = scheme_svc.run_automation(self.scheme, force=True)
        c = r["changes"][0]
        self.assertEqual(c["to"], SchemeMembership.Status.LAPSED)
        self.assertIn("arrears", c["reason"])
        self.assertIs(c["scheme"], self.scheme)


# ===========================================================================
# Views & permissions
# ===========================================================================

class Phase2ViewTests(Phase2Fixture):

    def setUp(self):
        super().setUp()
        profile_svc.install_builtins()
        self.m = self._enrol(self.mary, days_ago=200)

    def test_the_settings_area_and_profile_screens_load(self):
        self.client.force_login(self.treasurer)
        p = PolicyProfile.objects.first()
        for url in [reverse("benevolent_settings"),
                    reverse("benevolent_profile_list"),
                    reverse("benevolent_profile_detail", args=[p.pk]),
                    reverse("benevolent_wizard_start"),
                    reverse("benevolent_membership_detail", args=[self.m.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_every_wizard_section_renders_including_the_review(self):
        self.client.force_login(self.treasurer)
        for step in range(len(wizard_svc.SECTIONS) + 1):
            r = self.client.get(reverse("benevolent_wizard", args=[step]))
            self.assertEqual(r.status_code, 200, f"step {step}")

    def test_an_assistant_cannot_reach_the_settings_area(self):
        self.client.force_login(self.clerk)
        r = self.client.get(reverse("benevolent_settings"))
        self.assertNotEqual(r.status_code, 200)

    def test_a_treasurer_without_the_committee_right_cannot_vote(self):
        """Sitting on the committee is its own right — a committee whose seats are
        held automatically by the treasurer is not a committee."""
        self._new_version(approval_mode=SchemePolicy.ApprovalMode.COMMITTEE,
                          committee_quorum=3, waiting_period_days=0)
        case = self._case(self.m)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)

        self.client.force_login(self.clerk)
        r = self.client.post(reverse("benevolent_case_vote", args=[case.pk]),
                             {"decision": "APPROVE"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(case.committee_approvals.count(), 0)

    def test_the_wizard_writes_nothing_until_the_review_is_confirmed(self):
        self.client.force_login(self.treasurer)
        before = SchemePolicy.objects.count()
        self.client.post(reverse("benevolent_wizard", args=[0]),
                         {"purpose": "BENEVOLENT"})
        self.client.post(reverse("benevolent_wizard", args=[1]),
                         {"funding": "FIXED_PERIODIC", "dues_amount": "250"})
        self.assertEqual(SchemePolicy.objects.count(), before)   # nothing yet

        r = self.client.post(
            reverse("benevolent_wizard", args=[len(wizard_svc.SECTIONS)]),
            {"scheme": self.scheme.pk, "effective_from": TODAY.isoformat()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SchemePolicy.objects.count(), before + 1)
        draft = SchemePolicy.objects.order_by("-id").first()
        self.assertEqual(draft.status, SchemePolicy.Status.DRAFT)   # a DRAFT
        self.assertEqual(draft.contribution_amount, Decimal("250"))
