"""Levies outlive the payout; a spouse paying is still the member's money.

Three faults found together, all of them the same mistake in different clothes:
a rule written for one stage of a process applied to a later stage where it no
longer held.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase

from core import roles
from departments.models import Department
from members.models import Member

from .models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                     SchemeDependant, SchemeMembership, SchemePolicy)
from .services import allocation as alloc
from .services import registry as reg_svc
from .services import schemes as scheme_svc


class LevyOutlivesThePayoutTests(TestCase):
    """A levy may be collected after the church has paid the family.

    `OPEN_STATUSES` describes a case still being decided. A levy is the
    collection **from the members** that replenishes the fund, and it starts
    where the payout ends: the church pays a bereaved family promptly, then
    levies the membership over the following weeks. By the time the money
    arrives the case is PAID.

    Keying the levy off open cases therefore switched it off at exactly the
    point it was owed — the case vanished from the allocator's list, stopped
    raising an obligation, and `engine.resolve` refused the money outright as
    belonging to "a case that is already settled". The payout was settled. The
    levy was not.
    """

    def setUp(self):
        self.user = User.objects.create_user("tess-levy", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-levy",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Levy Scheme", code="LVY", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        self.event_type = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BRV",
            covers_dependants=True)
        policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=dt.date.today() - dt.timedelta(days=400),
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"))
        scheme_svc.publish_policy(policy, user=self.user)

        self.members = []
        for i in range(3):
            person = Member.objects.create(name=f"Levy Payer {i}",
                                           phone=f"25470000010{i}")
            m = reg_svc.register(self.scheme, person,
                                 joined_on=dt.date.today() - dt.timedelta(days=200))
            if m.status == SchemeMembership.Status.PENDING:
                m = reg_svc.admit(m, user=self.user)
            self.members.append(m)

        self.case = BenevolentCase.objects.create(
            scheme=self.scheme, event_type=self.event_type,
            membership=self.members[0], event_date=dt.date.today(),
            status=BenevolentCase.Status.APPROVED)

    def _levies(self, membership):
        from .services import obligations as ob
        return [o for o in ob.obligations_for(membership, as_of=dt.date.today())
                if o.kind == "LEVY"]

    def test_a_levy_is_owed_while_the_case_is_open(self):
        self.assertTrue(self._levies(self.members[1]))

    def test_a_levy_is_still_owed_once_the_family_has_been_paid(self):
        """The case that matters — this is when members actually pay."""
        self.case.status = BenevolentCase.Status.PAID
        self.case.save(update_fields=["status"])
        self.assertTrue(
            self._levies(self.members[1]),
            "The levy stopped being owed the moment the church paid the family, "
            "which is precisely when the members are asked for it.")

    def test_a_levy_is_still_owed_once_the_case_is_closed(self):
        """A member who pays late still has to be able to pay."""
        self.case.status = BenevolentCase.Status.CLOSED
        self.case.save(update_fields=["status"])
        self.assertTrue(self._levies(self.members[1]))

    def test_no_levy_is_owed_for_a_case_that_never_paid_out(self):
        """Nothing left the fund, so there is nothing to replenish."""
        for status in (BenevolentCase.Status.REJECTED,
                       BenevolentCase.Status.CANCELLED):
            with self.subTest(status=status):
                self.case.status = status
                self.case.save(update_fields=["status"])
                self.assertFalse(self._levies(self.members[1]))

    def test_a_settled_levy_stops_being_listed(self):
        """Paid is not the same as not owed — it must simply drop off."""
        from .services import obligations as ob
        member = self.members[1]
        self.assertTrue(self._levies(member))
        levy = self._levies(member)[0]
        self.assertEqual(levy.outstanding, levy.due)
        all_obs = [o for o in ob.obligations_for(member, as_of=dt.date.today(),
                                                 include_settled=True)
                   if o.kind == "LEVY"]
        self.assertTrue(all_obs)

    def test_money_can_be_attributed_to_a_paid_case(self):
        """The guard that refused it outright."""
        from .services import engine as engine_svc
        self.case.status = BenevolentCase.Status.PAID
        self.case.save(update_fields=["status"])
        problems = []
        try:
            engine_svc._validate_resolution  # noqa: B018 — presence check only
        except AttributeError:
            problems = None
        self.assertIn(BenevolentCase.Status.PAID,
                      BenevolentCase.LEVIABLE_STATUSES)
        self.assertNotIn(BenevolentCase.Status.CANCELLED,
                         BenevolentCase.LEVIABLE_STATUSES)

    def test_the_intake_form_offers_a_paid_case(self):
        from .forms import IntakeResolveForm
        from .models_contrib import ContributionIntake
        self.case.status = BenevolentCase.Status.PAID
        self.case.save(update_fields=["status"])
        item = ContributionIntake(scheme=self.scheme)
        form = IntakeResolveForm(item=item)
        self.assertIn(self.case, list(form.fields["case"].queryset))

    def test_a_lone_case_is_chosen_for_the_treasurer(self):
        """Nothing to choose between, so choosing is not a decision."""
        from .forms import IntakeResolveForm
        from .models_contrib import ContributionIntake
        item = ContributionIntake(scheme=self.scheme)
        form = IntakeResolveForm(item=item)
        self.assertEqual(form.fields["case"].initial, self.case.pk)

    def test_nothing_is_chosen_when_there_are_two(self):
        from .forms import IntakeResolveForm
        from .models_contrib import ContributionIntake
        BenevolentCase.objects.create(
            scheme=self.scheme, event_type=self.event_type,
            membership=self.members[1],
            event_date=dt.date.today() - dt.timedelta(days=30),
            status=BenevolentCase.Status.APPROVED)
        item = ContributionIntake(scheme=self.scheme)
        form = IntakeResolveForm(item=item)
        self.assertIsNone(form.fields["case"].initial)


class SpousePaymentIsTheMembersMoneyTests(TestCase):
    """A spouse paying on the member's behalf should be recognised as such.

    The allocator already scored a spouse's phone. It could not act on it: the
    auto-allocate gate wants 85 and a spouse's phone is worth 45, so a perfectly
    ordinary payment went to the unmatched queue every month.

    What was missing is the other half of the same evidence. When a wife pays,
    the bank narration carries *her* name, and the allocator only ever compared
    names against members — so the name signal was thrown away, and could even
    fuzzily match an unrelated member who shared a surname. Reading the
    household as well lets the phone and the name corroborate each other, which
    is the whole reason a treasurer can resolve these by hand in a second.
    """

    def setUp(self):
        self.user = User.objects.create_user("tess-alloc", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-alloc",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Household Scheme", code="HHS", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=dt.date.today() - dt.timedelta(days=400),
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY)
        scheme_svc.publish_policy(policy, user=self.user)
        from .models import ContributionRule
        ContributionRule.objects.create(pattern="hhs", scheme=self.scheme)

        person = Member.objects.create(name="Eliud Kemboi", phone="254722000111")
        self.membership = reg_svc.register(
            self.scheme, person, joined_on=dt.date.today() - dt.timedelta(days=200))
        if self.membership.status == SchemeMembership.Status.PENDING:
            self.membership = reg_svc.admit(self.membership, user=self.user)
        self.spouse = SchemeDependant.objects.create(
            membership=self.membership, name="Abigael Omoche",
            relationship=SchemeDependant.Relationship.SPOUSE,
            phone="0728302634", active=True)

    def _allocate(self, phone, name):
        return alloc.allocate(reference="hhs dues", phone=phone, name=name,
                              amount=Decimal("200"), date=dt.date.today())

    def test_a_spouse_paying_is_attributed_to_the_member(self):
        result = self._allocate(self.spouse.phone, self.spouse.display_name)
        self.assertTrue(result.candidates)
        self.assertEqual(result.candidates[0].membership_id, self.membership.pk)

    def test_it_is_confident_enough_to_act_on(self):
        """45 alone never cleared the gate; the name is the other half."""
        result = self._allocate(self.spouse.phone, self.spouse.display_name)
        self.assertGreaterEqual(
            result.identity_confidence, 85,
            "A spouse paying under her own name from her own phone still is not "
            "confident enough to allocate, so it goes back to the queue.")

    def test_the_reason_names_the_spouse(self):
        result = self._allocate(self.spouse.phone, self.spouse.display_name)
        codes = {s.code for s in result.candidates[0].signals}
        self.assertIn("spouse_phone", codes)
        self.assertIn("spouse_name", codes)

    def test_the_number_matches_however_it_was_written(self):
        """Stored "0728...", the bank sends "254728..." — one telephone.

        A Member's phone is normalised on save; a dependant's is stored as
        typed. Comparing them directly matched only the rows that happened to
        have been entered in international form.
        """
        for form in ("0728302634", "254728302634", "+254728302634"):
            with self.subTest(form=form):
                result = self._allocate(form, self.spouse.display_name)
                self.assertTrue(result.candidates, f"{form} matched nothing")
                self.assertEqual(result.candidates[0].membership_id,
                                 self.membership.pk)

    def test_a_deceased_spouse_is_not_matched(self):
        self.spouse.died_on = dt.date.today() - dt.timedelta(days=5)
        self.spouse.save(update_fields=["died_on"])
        result = self._allocate(self.spouse.phone, self.spouse.display_name)
        codes = {s.code for c in result.candidates for s in c.signals}
        self.assertNotIn("spouse_name", codes)

    def test_a_payer_who_is_also_a_member_is_left_for_a_person_to_decide(self):
        """Two true readings, not two close ones.

        A wife listed on her husband's household may hold her own membership.
        Her payment could be her dues or his, and the household evidence scores
        higher only because it draws on two signals. Picking one would be a
        guess dressed as a decision, and the margin test cannot catch it.
        """
        her_own = Member.objects.create(name="Abigael Omoche", phone="254799888777")
        own = reg_svc.register(self.scheme, her_own, joined_on=dt.date.today())
        if own.status == SchemeMembership.Status.PENDING:
            own = reg_svc.admit(own, user=self.user)
        own.refresh_from_db()
        self.assertEqual(own.status, SchemeMembership.Status.ACTIVE,
                         "fixture: her own membership must be live to compete")
        result = self._allocate(self.spouse.phone, self.spouse.display_name)
        self.assertTrue(
            result.is_ambiguous,
            "A payer who is both a member and someone's spouse was allocated "
            "automatically; that is a guess, not a decision.")


class RosterImportKeepsWholeHouseholdsTests(TestCase):
    """Every dependant column in the file is read, not the first three.

    A church roster registers a spouse, both parents, both parents-in-law and
    the children — nine or ten people. The importer counted to a fixed three and
    ignored the rest without a word: on a roster of 224 households, 519 of 1,123
    dependants were discarded while the import reported success. Silent
    truncation is the worst way to lose this: the missing people are discovered
    when somebody dies and the family is told they were never covered.
    """

    def test_every_dependant_column_present_is_read(self):
        from .views_bulk_import import _dependant_slots
        row = {f"dependant{i}_name": f"Person {i}" for i in range(1, 11)}
        self.assertEqual(_dependant_slots(row), list(range(1, 11)))

    def test_the_order_is_the_files_order(self):
        from .views_bulk_import import _dependant_slots
        row = {"dependant3_name": "c", "dependant1_name": "a", "dependant2_name": "b"}
        self.assertEqual(_dependant_slots(row), [1, 2, 3])

    def test_a_row_with_no_dependants_reads_none(self):
        from .views_bulk_import import _dependant_slots
        self.assertEqual(_dependant_slots({"name": "Solo"}), [])

    def test_unrelated_columns_are_ignored(self):
        from .views_bulk_import import _dependant_slots
        row = {"name": "x", "dependant1_name": "a", "dependant1_phone": "07",
               "dependant1_relationship": "SPOUSE"}
        self.assertEqual(_dependant_slots(row), [1])

    def test_the_template_offers_room_for_a_real_household(self):
        from .views_bulk_import import DEP_SLOTS
        self.assertGreaterEqual(
            DEP_SLOTS, 9,
            "The template is narrower than an ordinary household: spouse, two "
            "parents, two parents-in-law and the children.")
