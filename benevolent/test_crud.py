"""CRUD audit — real gaps found: dependants, cases, and memberships all had
create/read but no update path (delete/cancel already existed correctly, as
a soft action, everywhere it should)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                               SchemeDependant, SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class CrudFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tcrud", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("ccrud", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.fund = Department.objects.create(
            name="Crud Fund", slug="crud-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Crud Scheme", code="CRD", fund=self.fund, created_by=self.treasurer)
        self.event = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        self.mary = Member.objects.create(name="Mary Crud", phone="254700111222")
        self.m = reg_svc.register(self.scheme, self.mary, joined_on=TODAY - dt.timedelta(days=90),
                                  user=self.treasurer)


# ===========================================================================
# Dependants
# ===========================================================================

class DependantCrudTests(CrudFixture):

    def setUp(self):
        super().setUp()
        self.dep = reg_svc.add_dependant(
            self.m, relationship=SchemeDependant.Relationship.SPOUSE,
            name="Original Name", user=self.treasurer)

    def test_update_dependant_corrects_a_typo(self):
        reg_svc.update_dependant(
            self.dep, relationship=SchemeDependant.Relationship.SPOUSE,
            name="Corrected Name", phone="254733999888", user=self.treasurer)
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.name, "Corrected Name")
        self.assertEqual(self.dep.phone, "254733999888")

    def test_update_dependant_does_not_touch_registered_on(self):
        original = self.dep.registered_on
        reg_svc.update_dependant(
            self.dep, relationship=SchemeDependant.Relationship.SPOUSE,
            name="Another Name", user=self.treasurer)
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.registered_on, original)

    def test_a_no_op_update_does_not_log_a_spurious_event(self):
        from benevolent.models import MembershipEvent
        before = self.m.events.filter(kind="NOTE").count()
        reg_svc.update_dependant(
            self.dep, relationship=self.dep.relationship, name=self.dep.name,
            phone=self.dep.phone, user=self.treasurer)
        self.assertEqual(self.m.events.filter(kind="NOTE").count(), before)

    def test_the_edit_view_updates_the_dependant(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_household", args=[self.m.pk]),
            {"edit": self.dep.pk, "relationship": "SPOUSE", "name": "Web Edited Name",
            "phone": "254711000222", "registered_on": TODAY.isoformat()})
        self.assertEqual(r.status_code, 302)
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.name, "WEB EDITED NAME" if False else "Web Edited Name")
        self.assertEqual(self.dep.phone, "254711000222")

    def test_the_membership_page_offers_an_edit_link_per_dependant(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_membership_detail", args=[self.m.pk])).content.decode()
        self.assertIn("Edit details", body)
        self.assertIn(f'value="{self.dep.pk}"', body)


# ===========================================================================
# Cases
# ===========================================================================

class CaseCrudTests(CrudFixture):

    def setUp(self):
        super().setUp()
        self.case = case_svc.create_case(
            self.scheme, event_type=self.event, membership=self.m,
            event_date=TODAY - dt.timedelta(days=2), description="Original description",
            user=self.clerk)

    def test_a_draft_case_can_be_edited(self):
        case_svc.update_case(self.case, description="Corrected description",
                             claimed_amount=Decimal("5000"), user=self.treasurer)
        self.case.refresh_from_db()
        self.assertEqual(self.case.description, "Corrected description")
        self.assertEqual(self.case.claimed_amount, Decimal("5000"))

    def test_a_submitted_case_cannot_be_edited(self):
        case_svc.submit_case(self.case, user=self.clerk)
        with self.assertRaises(Exception):
            case_svc.update_case(self.case, description="Too late", user=self.treasurer)

    def test_the_edit_view_only_works_for_a_draft(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_case_edit", args=[self.case.pk]),
            {"event_type": self.event.pk, "event_date": TODAY.isoformat(),
            "reported_date": TODAY.isoformat(), "description": "Edited via view",
            "membership": self.m.pk})
        self.assertEqual(r.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.description, "Edited via view")

    def test_the_edit_view_refuses_a_non_draft_case(self):
        case_svc.submit_case(self.case, user=self.clerk)
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_case_edit", args=[self.case.pk]))
        self.assertEqual(r.status_code, 302)   # bounced back with an error message
        self.assertNotIn(
            reverse("benevolent_case_edit", args=[self.case.pk]),
            self.client.get(reverse("benevolent_case_detail", args=[self.case.pk]))
            .content.decode())

    def test_the_case_detail_page_shows_edit_only_for_a_draft(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_detail", args=[self.case.pk])).content.decode()
        self.assertIn(reverse("benevolent_case_edit", args=[self.case.pk]), body)

        case_svc.submit_case(self.case, user=self.clerk)
        body2 = self.client.get(
            reverse("benevolent_case_detail", args=[self.case.pk])).content.decode()
        self.assertNotIn(reverse("benevolent_case_edit", args=[self.case.pk]), body2)

    def test_editing_leaves_a_note_on_the_case_history(self):
        case_svc.update_case(self.case, description="Changed", user=self.treasurer)
        note = self.case.events.filter(kind="NOTE").first()
        self.assertIsNotNone(note)
        self.assertIn("description", note.summary)


# ===========================================================================
# Membership basic details
# ===========================================================================

class MembershipEditCrudTests(CrudFixture):

    def test_editing_household_name_and_notes(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_membership_admin", args=[self.m.pk, "edit"]),
            {"household_name": "The Corrected Household", "notes": "Edited note.",
            "date_of_birth": ""})
        self.assertEqual(r.status_code, 302)
        self.m.refresh_from_db()
        self.assertEqual(self.m.household_name, "The Corrected Household")
        self.assertEqual(self.m.notes, "Edited note.")

    def test_editing_does_not_touch_status_or_joined_on(self):
        original_joined = self.m.joined_on
        original_status = self.m.status
        self.client.force_login(self.treasurer)
        self.client.post(
            reverse("benevolent_membership_admin", args=[self.m.pk, "edit"]),
            {"household_name": "New Name", "notes": "", "date_of_birth": ""})
        self.m.refresh_from_db()
        self.assertEqual(self.m.joined_on, original_joined)
        self.assertEqual(self.m.status, original_status)

    def test_the_membership_page_shows_the_edit_form(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_membership_detail", args=[self.m.pk])).content.decode()
        self.assertIn("Edit household name", body)


# ===========================================================================
# Enrol / Register consolidation
# ===========================================================================

class EnrolRegisterConsolidationTests(CrudFixture):

    def test_the_scheme_page_enrol_button_points_at_the_full_register_form(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_scheme_detail", args=[self.scheme.pk])).content.decode()
        self.assertIn(reverse("benevolent_register", args=[self.scheme.pk]), body)

    def test_the_old_simple_enrol_screen_redirects_to_the_full_one(self):
        """Not removed outright — an old bookmark should still work. But it
        now REDIRECTS to the full registration screen rather than rendering
        a second, strictly-inferior enrolment form (no households, no
        dependants, no off-roll registration). One job, one code path: the
        duplicate form it used to render is gone."""
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_enrol", args=[self.scheme.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"],
                         reverse("benevolent_register", args=[self.scheme.pk]))
