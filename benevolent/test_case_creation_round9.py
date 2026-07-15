"""Round 9, items 1 & 2 — a death opens a case, and the form is rethought.

A benevolent scheme exists FOR the death of a member or their family. So when a
death is recorded, the case should already be there — filled in with everything
the scheme knows — rather than waiting for someone to remember to type it at the
worst moment of a family's year. And the case FORM was backwards: it asked for
the member, then the dependant, then the relationship, when the relationship is
in the database, the member is derivable from the dependant, and the claimed
amount is fixed by the constitution. It asked a treasurer to retype what the
system knew, which is asking them to introduce a discrepancy.

Both approaches — whether a death auto-opens a case, and who the beneficiary
defaults to — are settings, with a sensible default, not a hardcoded rule.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType,
                               BenevolentScheme, BenevolentSettings,
                               SchemeDependant, SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class DeathCaseFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_dc", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

        fund = Department.objects.create(name="DC Fund", slug="dc-fund",
                                         fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent", code="DCB", fund=fund, created_by=self.treasurer)
        self.death_event = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER",
            triggers_on_death=True)
        # a second event type, to prove the marker (not the name) is what's used
        BenevolentEventType.objects.create(
            scheme=self.scheme, name="Hospitalisation", code="HOSP")
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=900),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"), registration_required=True,
            registration_fee=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("50000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        self.settings = BenevolentSettings.get()
        self.settings.auto_open_case_on_death = (
            BenevolentSettings.DeathCaseMode.ON_RECORD)
        self.settings.case_beneficiary_default = (
            BenevolentSettings.BeneficiaryDefault.DERIVE)
        self.settings.save()

    def enrol(self, name):
        m = Member.objects.create(name=name)
        return reg_svc.register(self.scheme, m,
                                joined_on=TODAY - dt.timedelta(days=400),
                                user=self.treasurer)


class AutoOpenOnDeathTests(DeathCaseFixture):
    def test_member_death_opens_a_draft_case_prefilled(self):
        m = self.enrol("SILAS ONYANGO")

        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)

        case = BenevolentCase.objects.filter(membership=m).first()
        self.assertIsNotNone(case, "recording a death should open a draft case")
        self.assertEqual(case.status, BenevolentCase.Status.DRAFT)
        self.assertEqual(case.event_type, self.death_event)
        self.assertEqual(case.beneficiary_name, "SILAS ONYANGO")
        # policy fixes the benefit at 50,000 → claimed and funding target both set
        self.assertEqual(case.claimed_amount, Decimal("50000"))
        self.assertEqual(case.funding_target, Decimal("50000"))

    def test_dependant_death_opens_a_case_for_the_dependant(self):
        m = self.enrol("ESTHER NALIAKA")
        dep = SchemeDependant.objects.create(
            membership=m, name="BABY NALIAKA",
            relationship=SchemeDependant.Relationship.CHILD)

        reg_svc.record_dependant_death(dep, died_on=TODAY, user=self.treasurer)

        case = BenevolentCase.objects.filter(dependant=dep).first()
        self.assertIsNotNone(case)
        self.assertEqual(case.membership, m)               # member derived
        self.assertEqual(case.beneficiary_name, "BABY NALIAKA")
        self.assertIn("Child to ESTHER NALIAKA", case.beneficiary_relationship)

    def test_off_setting_opens_nothing(self):
        self.settings.auto_open_case_on_death = BenevolentSettings.DeathCaseMode.OFF
        self.settings.save()
        m = self.enrol("QUIET CASE")

        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)

        self.assertFalse(BenevolentCase.objects.filter(membership=m).exists())

    def test_blank_beneficiary_setting_leaves_beneficiary_empty(self):
        self.settings.case_beneficiary_default = (
            BenevolentSettings.BeneficiaryDefault.BLANK)
        self.settings.save()
        m = self.enrol("NO DEFAULT")

        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)

        case = BenevolentCase.objects.filter(membership=m).first()
        self.assertIsNotNone(case)                          # still opens
        self.assertEqual(case.beneficiary_name, "")         # but blank

    def test_idempotent_no_second_case_for_the_same_death(self):
        m = self.enrol("ONCE ONLY")
        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)
        # a second call (e.g. a signal firing too) must not open a duplicate
        case_svc.open_case_for_death(scheme=self.scheme, membership=m,
                                     event_date=TODAY, user=self.treasurer)
        self.assertEqual(
            BenevolentCase.objects.filter(membership=m).count(), 1)

    def test_no_death_event_type_marked_opens_nothing_and_notes_why(self):
        self.death_event.triggers_on_death = False
        self.death_event.save()
        # rename so the name-based fallback can't guess it either
        self.death_event.name = "General welfare"
        self.death_event.save()
        m = self.enrol("UNMARKED SCHEME")

        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)

        self.assertFalse(BenevolentCase.objects.filter(membership=m).exists())
        note = m.events.filter(summary__icontains="no single event type").first()
        self.assertIsNotNone(note, "should leave a note explaining why not")


class FixedBenefitHelperTests(DeathCaseFixture):
    def test_fixed_mode_returns_the_amount(self):
        policy = self.scheme.current_policy
        self.assertEqual(policy.fixed_benefit_for(self.death_event),
                         Decimal("50000"))

    def test_percentage_mode_fixes_nothing(self):
        policy = self.scheme.current_policy
        policy.benefit_mode = SchemePolicy.BenefitMode.PERCENTAGE
        self.assertIsNone(policy.fixed_benefit_for(self.death_event))


class CaseFormTests(DeathCaseFixture):
    def test_form_prefills_and_locks_claimed_when_policy_fixes_it(self):
        from benevolent.forms import CaseForm
        initial = case_svc.derive_case_defaults(
            self.scheme, event_type=self.death_event)
        form = CaseForm(scheme=self.scheme, initial=initial)
        self.assertTrue(form.fields["claimed_amount"].disabled)

    def test_form_derives_member_and_relationship_from_dependant(self):
        from benevolent.forms import CaseForm
        m = self.enrol("DERIVE HERE")
        dep = SchemeDependant.objects.create(
            membership=m, name="SON HERE",
            relationship=SchemeDependant.Relationship.CHILD)

        form = CaseForm(scheme=self.scheme, data={
            "dependant": dep.pk, "event_type": self.death_event.pk,
            "event_date": TODAY.isoformat(),
            "reported_date": TODAY.isoformat()})
        self.assertTrue(form.is_valid(), form.errors)
        cd = form.cleaned_data
        self.assertEqual(cd["membership"], m)               # derived, not typed
        self.assertEqual(cd["claimed_amount"], Decimal("50000"))  # policy-fixed
        self.assertIn("Child to DERIVE HERE", cd["beneficiary_relationship"])

    def test_create_view_get_prefills_from_dependant_querystring(self):
        m = self.enrol("QS MEMBER")
        dep = SchemeDependant.objects.create(
            membership=m, name="QS CHILD",
            relationship=SchemeDependant.Relationship.CHILD)
        url = reverse("benevolent_case_new", args=[self.scheme.pk])
        r = self.client.get(url, {"dependant": dep.pk})
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        self.assertEqual(form.initial.get("dependant"), dep)
        self.assertEqual(form.initial.get("membership"), m)


class DependantSearchTests(DeathCaseFixture):
    def test_dependant_search_returns_dependants_and_members(self):
        m = self.enrol("SEARCHABLE MEMBER")
        SchemeDependant.objects.create(
            membership=m, name="SEARCHABLE CHILD",
            relationship=SchemeDependant.Relationship.CHILD)

        url = reverse("benevolent_dependant_search")
        r = self.client.get(url, {"scheme": self.scheme.pk, "q": "SEARCHABLE"})
        self.assertEqual(r.status_code, 200)
        data = r.json()["results"]
        kinds = {row["kind"] for row in data}
        self.assertIn("dependant", kinds)
        self.assertIn("member", kinds)
        dep_row = next(r for r in data if r["kind"] == "dependant")
        self.assertEqual(dep_row["member_name"], "SEARCHABLE MEMBER")
        self.assertIn("Child", dep_row["relationship_line"])


class DependantEditLinkTests(DeathCaseFixture):
    """Editing a dependant must not silently unlink them from their member —
    and a name-only dependant must be upgradable to a linked church member."""

    def test_editing_a_linked_dependant_keeps_the_link(self):
        m = self.enrol("HOUSEHOLD HEAD")
        spouse = Member.objects.create(name="LINKED SPOUSE")
        dep = SchemeDependant.objects.create(
            membership=m, member=spouse,
            relationship=SchemeDependant.Relationship.SPOUSE)

        url = reverse("benevolent_household", args=[m.pk])
        # the edit form now submits the member link
        self.client.post(url, {
            "edit": dep.pk, "member": spouse.pk, "name": "",
            "phone": "0722000000",
            "relationship": SchemeDependant.Relationship.SPOUSE})

        dep.refresh_from_db()
        self.assertEqual(dep.member, spouse, "edit silently unlinked the member")
        self.assertEqual(dep.phone, "0722000000")

    def test_name_only_dependant_can_be_upgraded_to_a_linked_member(self):
        m = self.enrol("HOUSEHOLD HEAD 2")
        dep = SchemeDependant.objects.create(
            membership=m, name="TYPED CHILD",
            relationship=SchemeDependant.Relationship.CHILD)
        child = Member.objects.create(name="TYPED CHILD")

        url = reverse("benevolent_household", args=[m.pk])
        self.client.post(url, {
            "edit": dep.pk, "member": child.pk, "name": "",
            "relationship": SchemeDependant.Relationship.CHILD})

        dep.refresh_from_db()
        self.assertEqual(dep.member, child)
