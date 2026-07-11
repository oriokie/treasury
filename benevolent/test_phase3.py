"""Phase 3 — Member Registry, Households & Standing.

Grouped around the claims Phase 3 makes:

  1. TWO AXES        the lifecycle is a human's; standing is a function's. Automation
                     is structurally incapable of overruling a treasurer, and a
                     computed standing can never be typed in.
  2. ONE REGISTRY    `members.Member` is the only record of a person. A dependant on
                     the roll is linked, not duplicated.
  3. STANDING        all nine standings, computed from the policy, with grace,
                     exemptions and missed-case inactivity actually working.
  4. THE LIFECYCLE   registration, admission, suspension, death, transfer,
                     reinstatement — each a decision, each reasoned, each logged.
  5. NO DISAGREEMENT the register and the claim decision must never differ about a
                     plain fact. They share their facts, so they cannot.
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
                               BenevolentSettings, MembershipEvent, MembershipExemption,
                               RegistrationType, SchemeDependant, SchemeMembership,
                               SchemeNominee, SchemePolicy, Standing)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import standing as standing_svc
from benevolent.services.eligibility import evaluate

TODAY = dt.date.today()


class RegistryFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t3", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c3", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

        self.fund = Department.objects.create(
            name="Registry Fund", slug="registry-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Registry Scheme", code="REG", fund=self.fund,
            created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER",
            covers_dependants=True)

        self.policy = self._policy()
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.mary = Member.objects.create(name="Mary Otieno", phone="254700000011")
        self.john = Member.objects.create(name="John Otieno", phone="254700000012")
        self.grace = Member.objects.create(name="Grace Otieno", phone="254700000013")

    def _policy(self, **kw):
        d = dict(scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
                 membership_required=True, waiting_period_days=30,
                 contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
                 contribution_amount=Decimal("100"),
                 contribution_frequency=SchemePolicy.Frequency.MONTHLY,
                 benefit_mode=SchemePolicy.BenefitMode.FIXED,
                 benefit_amount=Decimal("10000"),
                 arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT,
                 created_by=self.treasurer)
        d.update(kw)
        return SchemePolicy.objects.create(**d)

    def _new_version(self, effective_from=None, **kw):
        v = scheme_svc.new_version_from(
            self.policy,
            effective_from=effective_from or (TODAY - dt.timedelta(days=400)),
            user=self.treasurer)
        for k, val in kw.items():
            setattr(v, k, val)
        v.save()
        scheme_svc.publish_policy(v, user=self.treasurer)
        return v

    def _register(self, member, days_ago=200, **kw):
        return reg_svc.register(self.scheme, member,
                                joined_on=TODAY - dt.timedelta(days=days_ago),
                                user=self.treasurer, **kw)

    def _pay_all_dues(self, m):
        owed = contrib_svc.arrears_for(m)
        if owed > 0:
            contrib_svc.record_contribution(
                self.scheme, date=TODAY, amount=owed, membership=m,
                user=self.treasurer, period_label="")
        return owed


# ===========================================================================
# 1. TWO AXES
# ===========================================================================

class TwoAxisTests(RegistryFixture):

    def test_standing_is_computed_and_the_column_is_only_a_cache(self):
        m = self._register(self.mary)
        # someone tampers with the cache directly
        SchemeMembership.objects.filter(pk=m.pk).update(standing=Standing.GOOD)
        m.refresh_from_db()
        self.assertEqual(m.standing, Standing.GOOD)

        # recomputing restores the truth — a cache cannot lie for long
        standing_svc.refresh(m)
        m.refresh_from_db()
        self.assertEqual(m.standing, Standing.ARREARS)

    def test_automation_writes_only_to_standing_and_cannot_touch_the_lifecycle(self):
        """The central Phase 3 claim. Phase 2's job mutated `status`, kept safe by
        an allowlist someone had to remember. It now writes to a DIFFERENT COLUMN,
        so overruling a treasurer is not merely forbidden — it is impossible."""
        m = self._register(self.mary)
        reg_svc.suspend(m, user=self.treasurer, reason="Under investigation.")
        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.SUSPENDED)

        # the member pays everything off — a fact that, in Phase 2, would have had
        # the job set them back to ACTIVE, silently reversing a human's decision
        self._pay_all_dues(m)
        scheme_svc.run_automation(self.scheme, force=True)

        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.SUSPENDED)   # untouched
        self.assertEqual(m.standing, Standing.SUSPENDED)                # reflects it

    def test_the_lifecycle_dominates_the_computed_standing(self):
        """A deceased member is not 'in arrears'. Whatever a human decided about a
        membership outranks anything a calculation has to say about it."""
        m = self._register(self.mary, days_ago=400)
        self.assertGreater(contrib_svc.arrears_for(m), Decimal(0))

        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)
        m.refresh_from_db()
        self.assertEqual(m.standing, Standing.DECEASED)
        result = standing_svc.assess(m)
        self.assertIn("outranks", result.workings[0])

    def test_status_no_longer_carries_derived_values(self):
        """LAPSED / EXPIRED / INACTIVE were facts wearing a decision's clothes."""
        values = [v for v, _ in SchemeMembership.Status.choices]
        for gone in ("LAPSED", "EXPIRED", "INACTIVE", "EXPELLED"):
            self.assertNotIn(gone, values)
        for kept in ("PENDING", "ACTIVE", "SUSPENDED", "WITHDRAWN", "DECEASED", "CLOSED"):
            self.assertIn(kept, values)

    def test_standing_covers_all_nine_required_states(self):
        values = [v for v, _ in Standing.choices]
        for s in ("GOOD", "EXEMPT", "GRACE", "ARREARS", "SUSPENDED", "INACTIVE",
                  "WITHDRAWN", "DECEASED", "CLOSED"):
            self.assertIn(s, values, s)


# ===========================================================================
# 2. ONE REGISTRY — extend Members, never duplicate it
# ===========================================================================

class OneRegistryTests(RegistryFixture):

    def test_a_dependant_on_the_church_roll_is_LINKED_not_retyped(self):
        m = self._register(self.mary, registration_type=RegistrationType.HOUSEHOLD,
                           household_name="The Otieno household")
        dep = reg_svc.add_dependant(
            m, member=self.john, relationship=SchemeDependant.Relationship.SPOUSE,
            user=self.treasurer)
        self.assertEqual(dep.member, self.john)
        self.assertEqual(dep.name, "")            # NOT typed in a second time
        # the name comes from the member record (which normalises it), not from
        # anything typed here — which is the whole point
        self.assertEqual(dep.display_name, self.john.name)

        # and the one record stays the one record
        self.john.name = "John Otieno Jr"
        self.john.save()
        self.john.refresh_from_db()
        dep.refresh_from_db()
        self.assertEqual(dep.display_name, self.john.name)
        self.assertIn("JR", dep.display_name.upper())

    def test_a_dependant_not_on_the_roll_is_still_covered(self):
        """A young child, or a parent in the village. Nothing is lost by their not
        being a church member."""
        m = self._register(self.mary)
        dep = reg_svc.add_dependant(
            m, name="Baby Otieno", relationship=SchemeDependant.Relationship.CHILD,
            user=self.treasurer)
        self.assertIsNone(dep.member)
        self.assertEqual(dep.display_name, "Baby Otieno")

    def test_a_dependant_needs_a_name_or_a_link(self):
        m = self._register(self.mary)
        d = SchemeDependant(membership=m,
                            relationship=SchemeDependant.Relationship.CHILD)
        with self.assertRaises(ValidationError):
            d.full_clean(exclude=["membership"])

    def test_the_members_page_shows_welfare_standing_without_a_second_register(self):
        m = self._register(self.mary, registration_type=RegistrationType.HOUSEHOLD)
        reg_svc.add_dependant(m, member=self.john,
                              relationship=SchemeDependant.Relationship.SPOUSE,
                              user=self.treasurer)
        self.client.force_login(self.treasurer)

        body = self.client.get(f"/members/{self.mary.pk}/").content.decode()
        self.assertIn(m.number, body)

        # John holds no membership himself, but is COVERED by Mary's — and his own
        # member page says so, which is the whole point of one registry
        body = self.client.get(f"/members/{self.john.pk}/").content.decode()
        self.assertIn("Also covered under someone else", body)


# ===========================================================================
# 3. STANDING
# ===========================================================================

class StandingTests(RegistryFixture):

    def test_a_member_paid_up_is_in_good_standing(self):
        m = self._register(self.mary, days_ago=40)
        self._pay_all_dues(m)
        r = standing_svc.refresh(m)
        self.assertEqual(r.standing, Standing.GOOD)
        self.assertTrue(r.covered)

    def test_a_member_behind_is_in_arrears(self):
        m = self._register(self.mary, days_ago=300)
        r = standing_svc.refresh(m)
        self.assertEqual(r.standing, Standing.ARREARS)
        self.assertGreater(r.facts.arrears, Decimal(0))
        # …and, under this scheme's DEDUCT policy, they are STILL COVERED: the
        # benefit is paid and the arrears netted off. Being in arrears is a fact
        # about the member, not a verdict on their claim.
        self.assertTrue(r.covered)

    def test_arrears_only_stop_cover_where_the_policy_says_BLOCK(self):
        self._new_version(arrears_treatment=SchemePolicy.ArrearsTreatment.BLOCK,
                          arrears_block=True)
        m = self._register(self.mary, days_ago=300)
        r = standing_svc.refresh(m)
        self.assertEqual(r.standing, Standing.ARREARS)
        self.assertFalse(r.covered)

    def test_a_grace_period_COVERS_or_it_would_not_be_grace(self):
        """A grace period that did not cover would just be a politer word for
        arrears. Inside it the member is in GRACE and is covered; the eligibility
        engine agrees, and does not refuse the claim."""
        self._new_version(grace_period_days=60, waiting_period_days=0,
                          arrears_treatment=SchemePolicy.ArrearsTreatment.BLOCK,
                          arrears_block=True)
        # joined 40 days ago: one period has fallen due and is a few days late
        m = self._register(self.mary, days_ago=40)
        r = standing_svc.refresh(m)
        self.assertEqual(r.standing, Standing.GRACE, r.reason)
        self.assertTrue(r.covered)

        e = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        arrears = next(c for c in e.checks if c.code == "arrears")
        self.assertTrue(arrears.passed)
        self.assertIn("still covered", arrears.detail)

    def test_grace_runs_from_when_the_money_became_late_not_from_today(self):
        """Measuring from today would put every member permanently inside their
        grace period — which would make the grace period a way of never being in
        arrears at all."""
        self._new_version(grace_period_days=10, waiting_period_days=0)
        m = self._register(self.mary, days_ago=300)
        r = standing_svc.refresh(m)
        self.assertEqual(r.standing, Standing.ARREARS)
        self.assertGreater(r.facts.days_past_due, 10)

    def test_an_exempt_member_owes_nothing_ANYWHERE(self):
        """The test that matters. If exemptions were applied only in the standing
        engine, an exempt member would show as clear on the register and STILL have
        money docked from their bereavement payout."""
        m = self._register(self.mary, days_ago=300)
        self.assertGreater(contrib_svc.arrears_for(m), Decimal(0))

        ex = reg_svc.grant_exemption(
            m, kind=MembershipExemption.Kind.LIFE,
            reason="Founding member; excused by board resolution 2019/3.",
            from_date=TODAY - dt.timedelta(days=400), user=self.clerk)
        reg_svc.approve_exemption(ex, user=self.treasurer)
        m.refresh_from_db()

        # 1. the register
        self.assertEqual(m.standing, Standing.EXEMPT)
        # 2. what they owe
        self.assertEqual(contrib_svc.arrears_for(m), Decimal(0))
        # 3. and — the one that would have bitten — the benefit is NOT docked
        e = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertEqual(e.entitlement.amount, Decimal("10000"))
        self.assertEqual(e.entitlement.deductions, [])

    def test_an_unapproved_exemption_excuses_nobody(self):
        """Proposing that a member be excused does not excuse them."""
        m = self._register(self.mary, days_ago=300)
        reg_svc.grant_exemption(
            m, kind=MembershipExemption.Kind.HARDSHIP,
            reason="Lost his job.", user=self.clerk)
        standing_svc.refresh(m)
        m.refresh_from_db()
        self.assertEqual(m.standing, Standing.ARREARS)
        self.assertGreater(contrib_svc.arrears_for(m), Decimal(0))

    def test_an_age_exemption_needs_no_paperwork(self):
        self._new_version(exemption_age=70)
        m = self._register(
            self.mary, days_ago=300,
            date_of_birth=dt.date(TODAY.year - 75, 6, 1))
        r = standing_svc.refresh(m)
        self.assertEqual(r.standing, Standing.EXEMPT)
        self.assertIn("aged 75", r.reason)
        self.assertEqual(contrib_svc.arrears_for(m), Decimal(0))

    def test_inactive_beats_arrears_because_it_is_the_bigger_fact(self):
        self._new_version(inactivity_months=6,
                          inactivity_action=SchemePolicy.InactivityAction.FLAG)
        m = self._register(self.mary, days_ago=400)
        r = standing_svc.refresh(m)
        self.assertEqual(r.standing, Standing.INACTIVE)
        self.assertGreater(r.facts.arrears, Decimal(0))     # they ARE in arrears…
        self.assertIn("stopped contributing", r.workings[0])  # …but that is not the point

    def test_an_exemption_beats_everything_derived(self):
        m = self._register(self.mary, days_ago=500)
        self._new_version(inactivity_months=3,
                          inactivity_action=SchemePolicy.InactivityAction.FLAG)
        ex = reg_svc.grant_exemption(
            m, kind=MembershipExemption.Kind.SERVICE,
            reason="Serving as a pastor.",
            from_date=TODAY - dt.timedelta(days=500), user=self.clerk)
        reg_svc.approve_exemption(ex, user=self.treasurer)
        r = standing_svc.refresh(m)
        self.assertEqual(r.standing, Standing.EXEMPT)


class MissedCaseInactivityTests(RegistryFixture):
    """A levy scheme has no monthly dues to miss, so 'months since a contribution'
    sees nothing. `inactivity_missed_cases` is the measure that catches the member
    who never stands with a bereaved family, and then expects the family to stand
    with them."""

    def setUp(self):
        super().setUp()
        self.v2 = self._new_version(
            effective_from=TODAY - dt.timedelta(days=400),
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            arrears_block=False,
            inactivity_months=0,
            inactivity_missed_cases=2,
            inactivity_action=SchemePolicy.InactivityAction.LAPSE,
            waiting_period_days=0)
        self.payer = self._register(self.mary, days_ago=300)
        self.shirker = self._register(self.john, days_ago=300)
        self.other = self._register(self.grace, days_ago=300)

    def _levy_case(self, days_ago):
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=self.other,
            event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=days_ago),
            reported_date=TODAY - dt.timedelta(days=days_ago),
            raised_by=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("1000"), user=self.treasurer)
        return case

    def test_a_member_who_missed_the_last_two_levies_is_inactive(self):
        c1 = self._levy_case(60)
        c2 = self._levy_case(30)
        for c in (c1, c2):
            contrib_svc.record_contribution(
                self.scheme, date=c.event_date, amount=Decimal("500"),
                membership=self.payer, case=c, user=self.treasurer)

        self.assertEqual(standing_svc.missed_case_levies(self.payer), 0)
        self.assertEqual(standing_svc.missed_case_levies(self.shirker), 2)

        self.assertEqual(standing_svc.refresh(self.payer).standing, Standing.GOOD)
        r = standing_svc.refresh(self.shirker)
        self.assertEqual(r.standing, Standing.INACTIVE)
        self.assertIn("case levies", r.reason)

    def test_the_run_is_CONSECUTIVE_so_an_old_lapse_is_forgiven(self):
        """Someone who missed two levies two years ago and has paid every one since
        is not the problem this rule is for."""
        old1, old2 = self._levy_case(200), self._levy_case(180)
        recent = self._levy_case(20)
        contrib_svc.record_contribution(
            self.scheme, date=recent.event_date, amount=Decimal("500"),
            membership=self.shirker, case=recent, user=self.treasurer)
        self.assertEqual(standing_svc.missed_case_levies(self.shirker), 0)
        self.assertEqual(standing_svc.refresh(self.shirker).standing, Standing.GOOD)

    def test_a_member_is_not_marked_down_for_their_own_bereavement(self):
        """They were never levied for it. Counting it as a miss would punish them
        for being bereaved."""
        mine = BenevolentCase.objects.create(
            scheme=self.scheme, membership=self.shirker,
            event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=20),
            reported_date=TODAY - dt.timedelta(days=20), raised_by=self.clerk)
        case_svc.submit_case(mine, user=self.clerk)
        case_svc.assess_case(mine, user=self.treasurer)
        case_svc.approve_case(mine, amount=Decimal("1000"), user=self.treasurer)
        self.assertEqual(standing_svc.missed_case_levies(self.shirker), 0)

    def test_the_eligibility_engine_refuses_a_shirker(self):
        self._levy_case(60)
        self._levy_case(30)
        r = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=self.shirker)
        self.assertFalse(r.eligible)
        inact = next(c for c in r.checks if c.code == "inactivity")
        self.assertIn("case levy", inact.detail)


# ===========================================================================
# 4. HOUSEHOLDS, LIFECYCLE, TRANSFER
# ===========================================================================

class HouseholdTests(RegistryFixture):

    def test_a_household_registration_covers_a_spouse_and_dependants(self):
        m = reg_svc.register(
            self.scheme, self.mary, joined_on=TODAY - dt.timedelta(days=200),
            user=self.treasurer, registration_type=RegistrationType.HOUSEHOLD,
            spouse=self.john,
            dependants=[{"name": "Child A",
                         "relationship": SchemeDependant.Relationship.CHILD}])
        self.assertEqual(m.registration_type, RegistrationType.HOUSEHOLD)
        self.assertIn("OTIENO", m.household_name.upper())
        self.assertTrue(m.household_name.lower().startswith("the "))
        people = reg_svc.household_members(m)
        self.assertEqual(len(people), 3)
        self.assertEqual(people[0]["role"], "Principal member")
        self.assertTrue(any(p["member"] == self.john for p in people))

    def test_only_one_spouse(self):
        m = self._register(self.mary, registration_type=RegistrationType.HOUSEHOLD)
        reg_svc.add_dependant(m, member=self.john,
                              relationship=SchemeDependant.Relationship.SPOUSE)
        with self.assertRaises(ValidationError):
            reg_svc.add_dependant(m, name="Someone else",
                                  relationship=SchemeDependant.Relationship.SPOUSE)

    def test_the_household_size_cap_counts_the_principal_member(self):
        self._new_version(max_household_size=3)
        m = self._register(self.mary, registration_type=RegistrationType.HOUSEHOLD)
        reg_svc.add_dependant(m, member=self.john,
                              relationship=SchemeDependant.Relationship.SPOUSE)
        reg_svc.add_dependant(m, name="Child A",
                              relationship=SchemeDependant.Relationship.CHILD)
        with self.assertRaises(ValidationError) as cm:
            reg_svc.add_dependant(m, name="Child B",
                                  relationship=SchemeDependant.Relationship.CHILD)
        self.assertIn("at most 3", str(cm.exception))

    def test_removing_a_dependant_does_not_destroy_cover_already_earned(self):
        """A claim already earned is not taken away by a later change to the
        household."""
        m = self._register(self.mary, days_ago=300,
                           registration_type=RegistrationType.HOUSEHOLD)
        dep = reg_svc.add_dependant(
            m, name="Child A", relationship=SchemeDependant.Relationship.CHILD,
            registered_on=TODAY - dt.timedelta(days=250))
        reg_svc.remove_dependant(dep, on=TODAY, user=self.treasurer)
        dep.refresh_from_db()

        self.assertFalse(dep.active)
        self.assertTrue(dep.covered_on(TODAY - dt.timedelta(days=100)))  # still
        self.assertFalse(dep.covered_on(TODAY))


class LifecycleTests(RegistryFixture):

    def test_every_lifecycle_change_needs_a_reason_and_is_logged(self):
        m = self._register(self.mary)
        with self.assertRaises(ValidationError):
            reg_svc.suspend(m, user=self.treasurer, reason="")

        reg_svc.suspend(m, user=self.treasurer, reason="Under investigation.")
        e = m.events.filter(kind=MembershipEvent.Kind.SUSPENDED).first()
        self.assertIsNotNone(e)
        self.assertEqual(e.reason, "Under investigation.")
        self.assertEqual(e.actor, self.treasurer)
        self.assertFalse(e.automated)

    def test_registration_is_logged_from_the_start(self):
        m = self._register(self.mary)
        self.assertTrue(m.events.filter(kind=MembershipEvent.Kind.ENROLLED).exists())

    def test_a_standing_change_is_logged_and_marked_automatic(self):
        m = self._register(self.mary, days_ago=300)
        e = m.events.filter(kind=MembershipEvent.Kind.STANDING).first()
        self.assertIsNotNone(e)
        self.assertEqual(e.to_value, Standing.ARREARS)

        m.events.all().delete()
        self._pay_all_dues(m)
        standing_svc.refresh(m)          # no user: a job did it
        e = m.events.filter(kind=MembershipEvent.Kind.STANDING).first()
        self.assertTrue(e.automated)
        self.assertEqual(e.to_value, Standing.GOOD)

    def test_recording_a_death_does_NOT_close_the_membership(self):
        """Their own death is very often the last claim on the scheme — the thing
        they paid in for. A system that closed the membership here would discard a
        family's entitlement at the exact moment it fell due."""
        m = self._register(self.mary, days_ago=300)
        reg_svc.record_death(m, died_on=TODAY - dt.timedelta(days=2),
                             user=self.treasurer)
        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.DECEASED)
        self.assertIsNone(m.left_on)

        # and the claim goes through
        r = evaluate(self.scheme, event_type=self.bereavement,
                     event_date=TODAY - dt.timedelta(days=2), membership=m)
        member_check = next(c for c in r.checks if c.code == "membership")
        self.assertTrue(member_check.passed)
        self.assertIn("does not bar it", member_check.detail)

    def test_a_deceased_member_cannot_be_reinstated(self):
        m = self._register(self.mary)
        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)
        with self.assertRaises(ValidationError) as cm:
            reg_svc.reinstate(m, user=self.treasurer)
        self.assertIn("transfer", str(cm.exception).lower())

    def test_refusing_a_registration_requires_a_reason(self):
        self._new_version(
            registration_required=True,
            registration_approval=SchemePolicy.RegistrationApproval.TREASURER)
        m = self._register(self.mary)
        self.assertEqual(m.status, SchemeMembership.Status.PENDING)
        with self.assertRaises(ValidationError):
            reg_svc.refuse(m, user=self.treasurer, reason="")
        reg_svc.refuse(m, user=self.treasurer, reason="Not a member of this church.")
        m.refresh_from_db()
        self.assertEqual(m.status, SchemeMembership.Status.CLOSED)


class TransferTests(RegistryFixture):

    def test_a_transfer_KEEPS_the_joining_date(self):
        """The whole point. A widow whose husband paid in for eleven years is not a
        new member with a ninety-day wait before the scheme will help her."""
        m = self._register(self.mary, days_ago=4000)   # ~11 years
        reg_svc.add_dependant(m, member=self.john,
                              relationship=SchemeDependant.Relationship.SPOUSE)
        reg_svc.record_death(m, died_on=TODAY - dt.timedelta(days=5),
                             user=self.treasurer)

        new = reg_svc.transfer(m, self.john, user=self.treasurer,
                               reason="Surviving spouse succeeds to the membership.")
        self.assertEqual(new.joined_on, m.joined_on)
        self.assertIsNone(new.reinstated_on)      # NOT a fresh waiting period
        self.assertEqual(new.cover_from, m.cover_from)
        self.assertEqual(new.succeeded_from, self.mary)

        # so she can claim at once
        e = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=new)
        wait = next(c for c in e.checks if c.code == "waiting_period")
        self.assertTrue(wait.passed)

    def test_the_trail_is_intact_in_both_directions(self):
        m = self._register(self.mary, days_ago=1000)
        new = reg_svc.transfer(m, self.john, user=self.treasurer, reason="Succession.")
        m.refresh_from_db()
        self.assertEqual(m.transferred_to, new)
        self.assertEqual(new.succeeded_from, self.mary)
        self.assertTrue(m.events.filter(
            kind=MembershipEvent.Kind.TRANSFERRED_OUT).exists())
        self.assertTrue(new.events.filter(
            kind=MembershipEvent.Kind.TRANSFERRED_IN).exists())

    def test_the_household_travels_with_the_membership(self):
        """The dependants were the household's, not the deceased's personally."""
        m = self._register(self.mary, days_ago=1000,
                           registration_type=RegistrationType.HOUSEHOLD)
        reg_svc.add_dependant(m, member=self.john,
                              relationship=SchemeDependant.Relationship.SPOUSE)
        reg_svc.add_dependant(m, name="Child A",
                              relationship=SchemeDependant.Relationship.CHILD,
                              registered_on=TODAY - dt.timedelta(days=900))
        new = reg_svc.transfer(m, self.john, user=self.treasurer, reason="Succession.")

        names = [d.display_name for d in new.dependants.all()]
        self.assertIn("Child A", names)
        self.assertNotIn("John Otieno", names)     # not his own dependant
        child = new.dependants.get(name="Child A")
        self.assertEqual(child.registered_on, TODAY - dt.timedelta(days=900))

    def test_a_policy_may_forbid_transfers(self):
        self._new_version(allow_transfers=False)
        m = self._register(self.mary, days_ago=1000)
        with self.assertRaises(ValidationError) as cm:
            reg_svc.transfer(m, self.john, user=self.treasurer, reason="x")
        self.assertIn("register afresh", str(cm.exception))

    def test_nobody_holds_two_memberships_in_one_scheme(self):
        m = self._register(self.mary, days_ago=1000)
        self._register(self.john, days_ago=100)
        with self.assertRaises(ValidationError) as cm:
            reg_svc.transfer(m, self.john, user=self.treasurer, reason="x")
        self.assertIn("cannot hold two", str(cm.exception))

    def test_reinstatement_still_restarts_the_waiting_period(self):
        """A transfer is not a reinstatement. The anti-gaming rule must survive
        Phase 3 intact: a lapsed member coming back is not a grieving widow."""
        self._new_version(reinstatement_waiting_days=90, waiting_period_days=30)
        m = self._register(self.mary, days_ago=1000)
        reg_svc.withdraw(m, user=self.treasurer, reason="Left the church.")
        reg_svc.reinstate(m, on=TODAY - dt.timedelta(days=5), user=self.treasurer)
        m.refresh_from_db()
        self.assertEqual(m.cover_from, TODAY - dt.timedelta(days=5))

        e = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        wait = next(c for c in e.checks if c.code == "waiting_period")
        self.assertFalse(wait.passed)
        self.assertIn("from reinstatement", wait.detail)


class ExemptionControlTests(RegistryFixture):

    def test_an_exemption_needs_a_second_person_to_approve_it(self):
        """It relieves a member of an obligation everyone else is carrying — that is
        a money decision, and it takes two."""
        m = self._register(self.mary)
        ex = reg_svc.grant_exemption(
            m, kind=MembershipExemption.Kind.HARDSHIP,
            reason="Lost his job.", user=self.treasurer)
        with self.assertRaises(ValidationError) as cm:
            reg_svc.approve_exemption(ex, user=self.treasurer)
        self.assertIn("other than the person who proposed", str(cm.exception))

    def test_an_exemption_must_record_why(self):
        m = self._register(self.mary)
        with self.assertRaises(ValidationError) as cm:
            reg_svc.grant_exemption(m, kind=MembershipExemption.Kind.OTHER,
                                    reason="  ", user=self.clerk)
        self.assertIn("favouritism", str(cm.exception))

    def test_a_policy_may_forbid_exemptions_altogether(self):
        self._new_version(allow_exemptions=False)
        m = self._register(self.mary)
        with self.assertRaises(ValidationError):
            reg_svc.grant_exemption(m, kind=MembershipExemption.Kind.LIFE,
                                    reason="Founder.", user=self.clerk)

    def test_a_levy_exempt_member_is_off_the_levy_roster(self):
        """Leaving them on it would chase them for money the church has already
        decided, in writing, that they do not owe."""
        v2 = self._new_version(
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            arrears_block=False, waiting_period_days=0)
        m1 = self._register(self.mary, days_ago=200)
        m2 = self._register(self.john, days_ago=200)
        m3 = self._register(self.grace, days_ago=200)

        ex = reg_svc.grant_exemption(
            m2, kind=MembershipExemption.Kind.LIFE, reason="Founder.",
            from_date=TODAY - dt.timedelta(days=300),
            exempt_dues=True, exempt_levies=True, user=self.clerk)
        reg_svc.approve_exemption(ex, user=self.treasurer)

        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=m1, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=1), reported_date=TODAY,
            raised_by=self.clerk)
        levy = contrib_svc.raise_case_levy(case)
        levied = [r["membership"].pk for r in levy["rows"]]
        self.assertNotIn(m2.pk, levied)      # exempt from levies
        self.assertNotIn(m1.pk, levied)      # the bereaved member
        self.assertIn(m3.pk, levied)


# ===========================================================================
# 5. THE REGISTER AND THE DECISION MUST NOT DISAGREE
# ===========================================================================

class NoDisagreementTests(RegistryFixture):

    def test_standing_and_eligibility_agree_about_the_arrears_figure(self):
        """They answer different questions, but they must not differ about a plain
        fact. They share their facts, so they cannot."""
        m = self._register(self.mary, days_ago=300)
        facts = standing_svc.facts_for(m)
        e = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        # the DEDUCT policy takes the arrears off the benefit — the same figure the
        # register is showing
        self.assertEqual(e.entitlement.amount, Decimal("10000") - facts.arrears)
        self.assertIn(str(facts.arrears), e.entitlement.deductions[0])

    def test_in_arrears_does_NOT_mean_the_claim_is_refused(self):
        """Standing reports; the policy decides. Under DEDUCT — the commonest real
        rule — an ARREARS member is still paid."""
        m = self._register(self.mary, days_ago=300)
        standing_svc.refresh(m)
        m.refresh_from_db()
        self.assertEqual(m.standing, Standing.ARREARS)

        e = evaluate(self.scheme, event_type=self.bereavement, event_date=TODAY,
                     membership=m)
        self.assertTrue(e.eligible)                     # not refused
        self.assertGreater(e.entitlement.amount, Decimal(0))

    def test_the_registers_view_of_cover_matches_the_engines_across_every_policy(self):
        """The guarantee, stated exhaustively.

        `StandingResult.covered` is what the register shows a treasurer. The
        eligibility engine is what actually decides a claim. If they ever
        disagreed, the module would be lying to somebody — so this walks every
        arrears treatment and every inactivity action and asserts they never do.
        """
        combos = [
            (SchemePolicy.ArrearsTreatment.BLOCK, SchemePolicy.InactivityAction.NONE),
            (SchemePolicy.ArrearsTreatment.DEDUCT, SchemePolicy.InactivityAction.NONE),
            (SchemePolicy.ArrearsTreatment.IGNORE, SchemePolicy.InactivityAction.NONE),
            (SchemePolicy.ArrearsTreatment.IGNORE, SchemePolicy.InactivityAction.FLAG),
            (SchemePolicy.ArrearsTreatment.IGNORE, SchemePolicy.InactivityAction.LAPSE),
            (SchemePolicy.ArrearsTreatment.DEDUCT, SchemePolicy.InactivityAction.LAPSE),
        ]
        m = self._register(self.mary, days_ago=400)
        eff = TODAY - dt.timedelta(days=450)
        for treatment, action in combos:
            eff += dt.timedelta(days=1)
            self.policy = self._new_version(
                effective_from=eff,
                arrears_treatment=treatment,
                arrears_block=(treatment == SchemePolicy.ArrearsTreatment.BLOCK),
                inactivity_months=(6 if action != SchemePolicy.InactivityAction.NONE
                                   else 0),
                inactivity_action=action,
                waiting_period_days=0)

            result = standing_svc.assess(m)
            e = evaluate(self.scheme, event_type=self.bereavement,
                         event_date=TODAY, membership=m)
            self.assertEqual(
                result.covered, e.eligible,
                f"The register says covered={result.covered} ({result.standing}) but the "
                f"engine says eligible={e.eligible}, under {treatment}/{action}.")


# ===========================================================================
# Views & permissions
# ===========================================================================

class RegistryViewTests(RegistryFixture):

    def setUp(self):
        super().setUp()
        self.m = self._register(self.mary, days_ago=200,
                                registration_type=RegistrationType.HOUSEHOLD)

    def test_the_registry_screens_load(self):
        self.client.force_login(self.treasurer)
        for url in [reverse("benevolent_registry"),
                    reverse("benevolent_register", args=[self.scheme.pk]),
                    reverse("benevolent_membership_detail", args=[self.m.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_the_registry_can_be_filtered_by_standing(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_registry"),
                            {"standing": Standing.ARREARS})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.m.number, r.content.decode())

        r = self.client.get(reverse("benevolent_registry"),
                            {"standing": Standing.GOOD})
        self.assertNotIn(self.m.number, r.content.decode())

    def test_an_assistant_cannot_approve_an_exemption(self):
        ex = reg_svc.grant_exemption(
            self.m, kind=MembershipExemption.Kind.HARDSHIP,
            reason="Hardship.", user=self.treasurer)
        self.client.force_login(self.clerk)
        self.client.post(
            reverse("benevolent_exemption_decision", args=[ex.pk, "approve"]))
        ex.refresh_from_db()
        self.assertFalse(ex.is_approved)

    def test_a_lifecycle_change_through_the_view_is_logged(self):
        self.client.force_login(self.treasurer)
        self.client.post(
            reverse("benevolent_membership_lifecycle", args=[self.m.pk, "suspend"]),
            {"on": TODAY.isoformat(), "reason": "Under investigation."})
        self.m.refresh_from_db()
        self.assertEqual(self.m.status, SchemeMembership.Status.SUSPENDED)
        self.assertTrue(self.m.events.filter(
            kind=MembershipEvent.Kind.SUSPENDED, actor=self.treasurer).exists())
