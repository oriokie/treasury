"""Bug fixes and features requested directly by Edwin, outside the phase
sequence:

  1. The "Admit" button on a pending membership's own page 404'd into
     "Unknown action" — wired to a view that had no admit handler.
  2. split_siblings() (giving app) OR'd three match conditions instead of
     falling back through them, so marking one bank entry as a manual
     receipt could sweep in unrelated people's payments that merely shared
     a generic reference and date. Covered in giving/tests.py, not here.
  3. Member/membership search endpoints for the new type-ahead widgets on
     benevolent's register/contribution/case forms, gated correctly for
     Phase 9's scheme-specific roles (not the general, Treasurer/Assistant-
     only core.views.MemberSearchView).
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profile
from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class BugfixFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tbug", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Bugfix Fund", slug="bugfix-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Bugfix Scheme", code="BUG", fund=self.fund, created_by=self.treasurer)
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


# ===========================================================================
# 1. The admit button
# ===========================================================================

class AdmitButtonTests(BugfixFixture):

    def setUp(self):
        super().setUp()
        member = Member.objects.create(name="Pending Bug Member", phone="254700111222")
        self.m = SchemeMembership.objects.create(
            scheme=self.scheme, member=member, status=SchemeMembership.Status.PENDING,
            joined_on=TODAY)

    def test_the_membership_detail_page_points_admit_at_a_view_that_handles_it(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_membership_detail", args=[self.m.pk])).content.decode()
        self.assertIn(
            reverse("benevolent_membership_admin", args=[self.m.pk, "admit"]), body)
        self.assertNotIn(
            reverse("benevolent_membership_lifecycle", args=[self.m.pk, "admit"]), body)

    def test_clicking_admit_actually_admits_the_member(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_membership_admin", args=[self.m.pk, "admit"]),
            {"on": TODAY.isoformat(), "reason": "Paperwork complete."})
        self.assertEqual(r.status_code, 302)
        self.m.refresh_from_db()
        self.assertEqual(self.m.status, SchemeMembership.Status.ACTIVE)

    def test_the_old_wrong_url_correctly_reports_unknown_action(self):
        """Confirms the diagnosis: the OTHER view genuinely has no admit
        branch, which is why the miswired button produced exactly the
        'Unknown action' error reported."""
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_membership_lifecycle", args=[self.m.pk, "admit"]),
            {"on": TODAY.isoformat(), "reason": "x"})
        self.assertEqual(r.status_code, 302)
        self.m.refresh_from_db()
        self.assertEqual(self.m.status, SchemeMembership.Status.PENDING)   # unchanged

    def test_the_reason_and_date_typed_into_the_shared_form_are_honoured(self):
        self.client.force_login(self.treasurer)
        backdated = TODAY - dt.timedelta(days=3)
        self.client.post(
            reverse("benevolent_membership_admin", args=[self.m.pk, "admit"]),
            {"on": backdated.isoformat(), "reason": "Backdated per committee minute."})
        self.m.refresh_from_db()
        self.assertEqual(self.m.registered_on, backdated)
        event = self.m.events.filter(kind="ADMITTED").first()
        self.assertEqual(event.reason, "Backdated per committee minute.")


# ===========================================================================
# 2. Member / membership search endpoints
# ===========================================================================

class SearchEndpointTests(BugfixFixture):

    def setUp(self):
        super().setUp()
        self.mary = Member.objects.create(name="Mary Wanjiru Search", phone="254711222999")
        self.john = Member.objects.create(name="John Otieno Search", phone="254722333888")
        self.enrolled = reg_svc.register(self.scheme, self.mary,
                                         joined_on=TODAY - dt.timedelta(days=30),
                                         user=self.treasurer)

    def test_member_search_finds_by_name(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_member_search"), {"q": "Wanjiru"})
        self.assertEqual(r.status_code, 200)
        names = [x["name"] for x in r.json()["results"]]
        self.assertIn("MARY WANJIRU SEARCH", names)   # Member.save() stores names uppercase

    def test_member_search_finds_by_phone(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_member_search"), {"q": "254722333"})
        names = [x["name"] for x in r.json()["results"]]
        self.assertIn("JOHN OTIENO SEARCH", names)

    def test_member_search_requires_at_least_two_characters(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_member_search"), {"q": "M"})
        self.assertEqual(r.json()["results"], [])

    def test_membership_search_only_returns_active_members_of_the_given_scheme(self):
        other_fund = Department.objects.create(
            name="Other Bugfix Fund", slug="other-bugfix-fund",
            fund_type=Department.FundType.LOCAL)
        other_scheme = BenevolentScheme.objects.create(
            name="Other Bugfix Scheme", code="OBUG", fund=other_fund,
            created_by=self.treasurer)
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_membership_search"),
                            {"q": "Wanjiru", "scheme": self.scheme.pk})
        names = [x["name"] for x in r.json()["results"]]
        self.assertIn("MARY WANJIRU SEARCH", names)

        # John never registered anywhere — must not appear
        r2 = self.client.get(reverse("benevolent_membership_search"),
                             {"q": "Otieno", "scheme": self.scheme.pk})
        self.assertEqual(r2.json()["results"], [])

        # Mary is not enrolled in the OTHER scheme
        r3 = self.client.get(reverse("benevolent_membership_search"),
                             {"q": "Wanjiru", "scheme": other_scheme.pk})
        self.assertEqual(r3.json()["results"], [])

    def test_membership_search_excludes_a_suspended_member(self):
        reg_svc.suspend(self.enrolled, user=self.treasurer, reason="Arrears.")
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_membership_search"),
                            {"q": "Wanjiru", "scheme": self.scheme.pk})
        self.assertEqual(r.json()["results"], [])

    def test_a_scheme_specific_role_with_no_treasurer_group_can_search(self):
        """The reason this is its own endpoint: a Registration Officer holds
        no Treasurer/Assistant group membership at all, and would be
        refused by the general, Treasurer/Assistant-only search."""
        officer = User.objects.create_user("regofficer_search", password="x")
        Profile.objects.get(
            name="Benevolent Registration Officer (default)").users.add(officer)
        self.client.force_login(officer)
        r = self.client.get(reverse("benevolent_member_search"), {"q": "Wanjiru"})
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get(reverse("benevolent_membership_search"),
                             {"q": "Wanjiru", "scheme": self.scheme.pk})
        self.assertEqual(r2.status_code, 200)

    def test_an_outsider_cannot_use_the_search_at_all(self):
        outsider = User.objects.create_user("outsider_search", password="x")
        self.client.force_login(outsider)
        r = self.client.get(reverse("benevolent_member_search"), {"q": "Wanjiru"})
        self.assertNotEqual(r.status_code, 200)


class FormWidgetWiringTests(BugfixFixture):
    """The forms actually load the new search widget, so the fields aren't
    silently left as giant unsearchable dropdowns."""

    def test_the_register_form_wires_the_search_widget(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_register", args=[self.scheme.pk])).content.decode()
        self.assertIn("benevolent-search.js", body)
        self.assertIn('selectId: "id_member"', body)

    def test_the_case_form_wires_the_search_widget_scoped_to_the_scheme(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_new", args=[self.scheme.pk])).content.decode()
        self.assertIn("benevolent-search.js", body)
        self.assertIn(f"scheme: {self.scheme.pk}", body)

    def test_the_contribution_form_wires_both_search_widgets(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_contribute", args=[self.scheme.pk])).content.decode()
        self.assertIn('selectId: "id_membership"', body)
        self.assertIn('selectId: "id_member"', body)


# ===========================================================================
# 3. Bulk membership import — "bring an existing roster in, already paid up"
# ===========================================================================

class BulkImportTests(BugfixFixture):

    def _upload(self, csv_text):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile("roster.csv", csv_text.encode(), content_type="text/csv")

    def test_template_download_has_the_right_columns(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                            {"template": "1"})
        self.assertEqual(r.status_code, 200)
        header = r.content.decode().splitlines()[0]
        for col in ("name", "phone", "joined_on", "registration_type",
                   "mark_paid_up", "dependant1_name", "dependant1_relationship",
                   "dependant1_phone"):
            self.assertIn(col, header)

    def test_a_simple_individual_row_creates_an_active_membership(self):
        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up\n"
                   "Grace Achieng,254711000111,2023-03-01,INDIVIDUAL,,0\n")
        self.client.force_login(self.treasurer)
        r = self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                             {"file": self._upload(csv_text)})
        self.assertEqual(r.status_code, 200)
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="GRACE ACHIENG")
        self.assertEqual(m.status, SchemeMembership.Status.ACTIVE)
        self.assertEqual(m.joined_on, dt.date(2023, 3, 1))

    def test_a_household_row_creates_dependants_with_phones(self):
        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up,"
                   "dependant1_name,dependant1_relationship,dependant1_phone\n"
                   "Samuel Kiplagat,254722000333,2022-01-10,HOUSEHOLD,The Kiplagat "
                   "Household,0,Ann Kiplagat,SPOUSE,254733000444\n")
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": self._upload(csv_text)})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="SAMUEL KIPLAGAT")
        self.assertEqual(m.registration_type, "HOUSEHOLD")
        dep = m.dependants.get()
        self.assertEqual(dep.relationship, "SPOUSE")
        self.assertEqual(dep.phone, "254733000444")

    def test_mark_paid_up_clears_arrears_with_a_visible_waiver(self):
        from benevolent.models import MemberAdjustment
        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up\n"
                   "Peter Mwangi,254700222111,2024-01-01,INDIVIDUAL,,1\n")
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": self._upload(csv_text)})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="PETER MWANGI")
        from benevolent.services import contributions as contrib_svc
        self.assertEqual(contrib_svc.arrears_for(m), 0)
        adj = MemberAdjustment.objects.filter(membership=m, kind="WAIVER").first()
        self.assertIsNotNone(adj)
        self.assertTrue(adj.automated)
        self.assertTrue(adj.is_effective)   # auto-approved, in force immediately
        self.assertIn("Migrated", adj.reason)

    def test_without_mark_paid_up_arrears_show_normally(self):
        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up\n"
                   "Susan Njeri,254700333222,2024-01-01,INDIVIDUAL,,0\n")
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": self._upload(csv_text)})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="SUSAN NJERI")
        from benevolent.services import contributions as contrib_svc
        self.assertGreater(contrib_svc.arrears_for(m), 0)

    def test_a_policy_requiring_approval_is_bypassed_since_this_is_a_migration(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=400), user=self.treasurer)
        v2.registration_required = True
        v2.registration_approval = SchemePolicy.RegistrationApproval.TREASURER
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up\n"
                   "Alice Nyambura,254700555666,2023-05-05,INDIVIDUAL,,0\n")
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": self._upload(csv_text)})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="ALICE NYAMBURA")
        self.assertEqual(m.status, SchemeMembership.Status.ACTIVE)   # not PENDING

    def test_no_welcome_notification_is_sent_for_an_imported_member(self):
        from benevolent.services import notify as notify_svc
        from benevolent.models import BenevolentNotification, NotificationEvent
        notify_svc.install_default_templates()
        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up\n"
                   "Daniel Kimani,254700777888,2023-02-02,INDIVIDUAL,,0\n")
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": self._upload(csv_text)})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="DANIEL KIMANI")
        self.assertFalse(BenevolentNotification.objects.filter(
            membership=m, event=NotificationEvent.REGISTRATION_CONFIRMED).exists())

    def test_someone_already_enrolled_is_reported_as_a_problem_row_not_duplicated(self):
        existing_member = Member.objects.create(name="Already Enrolled", phone="254700999000")
        reg_svc.register(self.scheme, existing_member, joined_on=TODAY, user=self.treasurer)

        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up\n"
                   "Already Enrolled,254700999000,2023-01-01,INDIVIDUAL,,0\n")
        self.client.force_login(self.treasurer)
        r = self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                             {"file": self._upload(csv_text)})
        self.assertEqual(r.context["imported"], 0)
        self.assertEqual(r.context["skipped"], 1)
        self.assertEqual(
            SchemeMembership.objects.filter(scheme=self.scheme, member=existing_member).count(),
            1)

    def test_reimporting_is_safe_to_repeat(self):
        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up\n"
                   "Repeat Import Test,254700111999,2023-01-01,INDIVIDUAL,,0\n")
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": self._upload(csv_text)})
        r2 = self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                              {"file": self._upload(csv_text)})
        self.assertEqual(r2.context["imported"], 0)
        self.assertEqual(r2.context["skipped"], 1)
        self.assertEqual(
            SchemeMembership.objects.filter(scheme=self.scheme,
                                            member__name="REPEAT IMPORT TEST").count(), 1)

    def test_a_registration_officer_can_use_bulk_import(self):
        officer = User.objects.create_user("regofficer_import", password="x")
        Profile.objects.get(
            name="Benevolent Registration Officer (default)").users.add(officer)
        csv_text = ("name,phone,joined_on,registration_type,household_name,mark_paid_up\n"
                   "Officer Import Test,254700444333,2023-01-01,INDIVIDUAL,,0\n")
        self.client.force_login(officer)
        r = self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                             {"file": self._upload(csv_text)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["imported"], 1)

    def test_a_finance_officer_cannot_use_bulk_import(self):
        """Bulk import is registration work, not finance work."""
        officer = User.objects.create_user("finofficer_import", password="x")
        Profile.objects.get(name="Benevolent Finance Officer (default)").users.add(officer)
        self.client.force_login(officer)
        r = self.client.get(reverse("benevolent_bulk_import", args=[self.scheme.pk]))
        self.assertNotEqual(r.status_code, 200)

    def test_dependant_phone_can_be_set_through_the_ordinary_household_form_too(self):
        """The gap this closed wasn't only in bulk import — add_dependant()
        itself never accepted a phone, despite the model field existing
        specifically for allocation matching."""
        member = Member.objects.create(name="Household Test Member", phone="254711222333")
        m = reg_svc.register(self.scheme, member, joined_on=TODAY, user=self.treasurer)
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_household", args=[m.pk]),
            {"relationship": "SPOUSE", "name": "Household Test Spouse",
            "phone": "254799888777", "registered_on": TODAY.isoformat()})
        self.assertEqual(r.status_code, 302)
        dep = m.dependants.get()
        self.assertEqual(dep.phone, "254799888777")


# ===========================================================================
# 3. Bulk roster import
# ===========================================================================

class BulkImportTests(BugfixFixture):

    def _csv(self, rows, header=None):
        import io
        header = header or ["name", "phone", "joined_on", "registration_type",
                            "household_name", "mark_paid_up",
                            "dependant1_name", "dependant1_relationship", "dependant1_phone",
                            "dependant2_name", "dependant2_relationship", "dependant2_phone",
                            "dependant3_name", "dependant3_relationship", "dependant3_phone"]
        lines = [",".join(header)]
        for r in rows:
            lines.append(",".join(r))
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile("roster.csv", "\n".join(lines).encode(), content_type="text/csv")

    def test_a_simple_individual_row_is_imported_active(self):
        self.client.force_login(self.treasurer)
        f = self._csv([[
            "Grace Kamau", "0722000111", "2023-01-15", "INDIVIDUAL", "", "0",
            "", "", "", "", "", "", "", "", ""]])
        r = self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                             {"file": f})
        self.assertEqual(r.status_code, 200)
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="GRACE KAMAU")
        self.assertEqual(m.status, SchemeMembership.Status.ACTIVE)
        self.assertEqual(m.joined_on, dt.date(2023, 1, 15))

    def test_a_household_row_imports_its_dependant(self):
        self.client.force_login(self.treasurer)
        f = self._csv([[
            "Mary Wanjiru", "0722111222", "2023-01-15", "HOUSEHOLD",
            "The Wanjiru Household", "0",
            "John Wanjiru", "SPOUSE", "0733444555", "", "", "", "", "", ""]])
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": f})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="MARY WANJIRU")
        self.assertEqual(m.registration_type, "HOUSEHOLD")
        dep = m.dependants.get()
        self.assertEqual(dep.name, "John Wanjiru")
        self.assertEqual(dep.relationship, "SPOUSE")
        self.assertEqual(dep.phone, "0733444555")

    def test_mark_paid_up_clears_arrears_with_a_visible_waiver(self):
        self.client.force_login(self.treasurer)
        f = self._csv([[
            "Old Member", "0700111000",
            (TODAY - dt.timedelta(days=400)).isoformat(), "INDIVIDUAL", "", "1",
            "", "", "", "", "", "", "", "", ""]])
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": f})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="OLD MEMBER")
        from benevolent.services import contributions as contrib_svc
        self.assertEqual(contrib_svc.arrears_for(m), 0)

        from benevolent.models import MemberAdjustment
        adj = MemberAdjustment.objects.get(membership=m)
        self.assertEqual(adj.kind, MemberAdjustment.Kind.WAIVER)
        self.assertTrue(adj.automated)
        self.assertTrue(adj.is_effective)
        self.assertIn("predates this system", adj.reason)

    def test_without_mark_paid_up_arrears_show_normally(self):
        self.client.force_login(self.treasurer)
        f = self._csv([[
            "Owing Member", "0700111001",
            (TODAY - dt.timedelta(days=400)).isoformat(), "INDIVIDUAL", "", "0",
            "", "", "", "", "", "", "", "", ""]])
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": f})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="OWING MEMBER")
        from benevolent.services import contributions as contrib_svc
        self.assertGreater(contrib_svc.arrears_for(m), 0)

    def test_no_welcome_notification_is_sent_for_an_import(self):
        """A member who has belonged for years must not get a 'welcome to
        the scheme' text on the day their historical record is imported."""
        self.client.force_login(self.treasurer)
        f = self._csv([[
            "Silent Member", "0700111002",
            (TODAY - dt.timedelta(days=100)).isoformat(), "INDIVIDUAL", "", "0",
            "", "", "", "", "", "", "", "", ""]])
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": f})
        from benevolent.models import BenevolentNotification
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="SILENT MEMBER")
        self.assertFalse(BenevolentNotification.objects.filter(membership=m).exists())

    def test_an_existing_active_member_is_reported_as_a_problem_row_not_duplicated(self):
        mary = Member.objects.create(name="Already Enrolled", phone="0700111003")
        reg_svc.register(self.scheme, mary, joined_on=TODAY - dt.timedelta(days=10),
                         user=self.treasurer)
        before = SchemeMembership.objects.filter(scheme=self.scheme).count()

        self.client.force_login(self.treasurer)
        f = self._csv([[
            "Already Enrolled", "0700111003", TODAY.isoformat(), "INDIVIDUAL", "", "0",
            "", "", "", "", "", "", "", "", ""]])
        r = self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                             {"file": f})
        self.assertContains(r, "already enrolled", status_code=200)
        self.assertEqual(
            SchemeMembership.objects.filter(scheme=self.scheme).count(), before)

    def test_a_bad_date_is_reported_without_sinking_the_whole_batch(self):
        self.client.force_login(self.treasurer)
        f = self._csv([
            ["Bad Date Row", "0700111004", "not-a-date", "INDIVIDUAL", "", "0",
             "", "", "", "", "", "", "", "", ""],
            ["Good Row", "0700111005", TODAY.isoformat(), "INDIVIDUAL", "", "0",
             "", "", "", "", "", "", "", "", ""],
        ])
        r = self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                             {"file": f})
        self.assertContains(r, "not-a-date", status_code=200)
        self.assertTrue(SchemeMembership.objects.filter(
            scheme=self.scheme, member__name="GOOD ROW").exists())
        self.assertFalse(SchemeMembership.objects.filter(
            scheme=self.scheme, member__name="BAD DATE ROW").exists())

    def test_the_csv_template_downloads(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                            {"template": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn(b"dependant1_name", r.content)

    def test_a_registration_officer_can_import_but_an_outsider_cannot(self):
        officer = User.objects.create_user("regofficer_import", password="x")
        Profile.objects.get(
            name="Benevolent Registration Officer (default)").users.add(officer)
        self.client.force_login(officer)
        f = self._csv([[
            "Officer Import", "0700111006", TODAY.isoformat(), "INDIVIDUAL", "", "0",
            "", "", "", "", "", "", "", "", ""]])
        r = self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                             {"file": f})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(SchemeMembership.objects.filter(
            scheme=self.scheme, member__name="OFFICER IMPORT").exists())

        outsider = User.objects.create_user("outsider_import", password="x")
        self.client.force_login(outsider)
        r2 = self.client.get(reverse("benevolent_bulk_import", args=[self.scheme.pk]))
        self.assertNotEqual(r2.status_code, 200)

    def test_reimporting_is_safe_and_does_not_duplicate_a_dependant(self):
        """Re-running the SAME file after fixing an unrelated row must not
        create a second copy of a household's dependant for the row that
        already succeeded — the existing-membership guard stops it at the
        membership level before add_dependant is ever reached again."""
        self.client.force_login(self.treasurer)
        f1 = self._csv([[
            "Repeat Row", "0700111007", TODAY.isoformat(), "HOUSEHOLD", "Repeat House", "0",
            "Repeat Spouse", "SPOUSE", "0700111008", "", "", "", "", "", ""]])
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": f1})
        f2 = self._csv([[
            "Repeat Row", "0700111007", TODAY.isoformat(), "HOUSEHOLD", "Repeat House", "0",
            "Repeat Spouse", "SPOUSE", "0700111008", "", "", "", "", "", ""]])
        self.client.post(reverse("benevolent_bulk_import", args=[self.scheme.pk]),
                         {"file": f2})
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="REPEAT ROW")
        self.assertEqual(m.dependants.count(), 1)


# ===========================================================================
# 4. Standing snapshot on the case page (dues-funded schemes)
# ===========================================================================

class StandingSnapshotTests(BugfixFixture):

    def _case_for(self, member, days_ago=200):
        m = reg_svc.register(self.scheme, member, joined_on=TODAY - dt.timedelta(days=days_ago),
                             user=self.treasurer)
        return m

    def test_scheme_standing_snapshot_groups_active_members_by_standing(self):
        from benevolent.services import reporting as report_svc
        from benevolent.services import standing as standing_svc
        mary = Member.objects.create(name="Snap Good", phone="0711000001")
        john = Member.objects.create(name="Snap Owing", phone="0711000002")
        m1 = self._case_for(mary, days_ago=5)     # too new to owe
        m2 = self._case_for(john, days_ago=400)   # long overdue

        # this scheme's arrears_treatment is IGNORE (from BugfixFixture), so
        # standing won't compute ARREARS from dues alone under that policy —
        # switch to DEDUCT so the snapshot has something real to group
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=450), user=self.treasurer)
        v2.arrears_treatment = SchemePolicy.ArrearsTreatment.DEDUCT
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)
        standing_svc.refresh(m1, user=self.treasurer)
        standing_svc.refresh(m2, user=self.treasurer)

        snap = report_svc.scheme_standing_snapshot(self.scheme)
        self.assertEqual(snap["total"], 2)
        all_ids = ({m.pk for m in snap["good"]} | {m.pk for m in snap["arrears"]}
                  | {m.pk for m in snap["grace"]} | {m.pk for m in snap["exempt"]}
                  | {m.pk for m in snap["inactive"]})
        self.assertEqual(all_ids, {m1.pk, m2.pk})   # every active member accounted for once

    def test_the_case_page_shows_the_standing_panel_for_a_dues_funded_scheme(self):
        mary = Member.objects.create(name="Panel Member", phone="0711000003")
        m = self._case_for(mary)
        from benevolent.models import BenevolentCase
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=m, event_type=self.event,
            event_date=TODAY, reported_date=TODAY, raised_by=self.treasurer)
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_detail", args=[case.pk])).content.decode()
        self.assertIn("standing right now", body)
        self.assertNotIn("Levy for this case", body)

    def test_the_case_page_shows_the_levy_panel_not_the_standing_panel_for_a_levy_scheme(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=450), user=self.treasurer)
        v2.contribution_mode = SchemePolicy.ContributionMode.PER_CASE_LEVY
        v2.levy_amount = Decimal("500")
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        mary = Member.objects.create(name="Levy Scheme Member", phone="0711000004")
        m = self._case_for(mary, days_ago=5)
        from benevolent.models import BenevolentCase
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=m, event_type=self.event,
            event_date=TODAY, reported_date=TODAY, raised_by=self.treasurer)
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_case_detail", args=[case.pk])).content.decode()
        self.assertIn("Levy for this case", body)
        self.assertNotIn("standing right now", body)


# ===========================================================================
# 5. Register without requiring an existing church roll member
# ===========================================================================

class RegisterIndependentOfChurchRollTests(BugfixFixture):

    def test_a_free_text_name_creates_and_registers_a_new_member(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_register", args=[self.scheme.pk]),
            {"member": "", "member_name": "Grace New Person",
             "member_phone": "254799333444", "registration_type": "INDIVIDUAL",
             "joined_on": TODAY.isoformat(), "notes": ""})
        self.assertEqual(r.status_code, 302)
        m = SchemeMembership.objects.get(scheme=self.scheme, member__name="GRACE NEW PERSON")
        self.assertEqual(m.member.phone, "254799333444")

    def test_neither_member_nor_name_shows_a_clear_error(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_register", args=[self.scheme.pk]),
            {"member": "", "member_name": "", "registration_type": "INDIVIDUAL",
             "joined_on": TODAY.isoformat(), "notes": ""})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Pick someone from the church roll, or type a name below.")

    def test_giving_both_a_member_and_a_free_text_name_is_rejected(self):
        member = Member.objects.create(name="Existing Roll Member", phone="254700999888")
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_register", args=[self.scheme.pk]),
            {"member": member.pk, "member_name": "Someone Else",
             "registration_type": "INDIVIDUAL",
             "joined_on": TODAY.isoformat(), "notes": ""})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not both")

    def test_a_second_registration_by_name_reuses_the_same_new_member_not_a_duplicate(self):
        self.client.force_login(self.treasurer)
        self.client.post(
            reverse("benevolent_register", args=[self.scheme.pk]),
            {"member": "", "member_name": "Repeat Person", "member_phone": "254711555000",
             "registration_type": "INDIVIDUAL",
             "joined_on": TODAY.isoformat(), "notes": ""})
        # a second scheme, same person by phone — must reuse the member record
        fund2 = Department.objects.create(
            name="Bugfix Fund 2", slug="bugfix-fund-2", fund_type=Department.FundType.LOCAL)
        scheme2 = BenevolentScheme.objects.create(
            name="Bugfix Scheme 2", code="BUG2", fund=fund2, created_by=self.treasurer)
        policy2 = SchemePolicy.objects.create(
            scheme=scheme2, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy2, user=self.treasurer)
        scheme_svc.activate_scheme(scheme2, user=self.treasurer)
        self.client.post(
            reverse("benevolent_register", args=[scheme2.pk]),
            {"member": "", "member_name": "Repeat Person", "member_phone": "254711555000",
             "registration_type": "INDIVIDUAL",
             "joined_on": TODAY.isoformat(), "notes": ""})
        self.assertEqual(Member.objects.filter(name="REPEAT PERSON").count(), 1)

    def test_the_hidden_selects_native_required_attribute_is_removed_by_the_widget(self):
        """The widget moves `required` to the visible search box, since a
        hidden <select> silently skips native browser validation — a real
        cause of "the button doesn't seem to do anything" when a required
        field was left empty."""
        js = open("static/js/benevolent-search.js").read()
        self.assertIn("select.required = false", js)
        self.assertIn("input.required = true", js)


# ===========================================================================
# 6. Marking a dependant as deceased
# ===========================================================================

class DependantDeceasedTests(BugfixFixture):

    def setUp(self):
        super().setUp()
        from benevolent.models import SchemeDependant
        mary = Member.objects.create(name="Household Head", phone="254700222333")
        self.m = reg_svc.register(self.scheme, mary, joined_on=TODAY - dt.timedelta(days=90),
                                  user=self.treasurer, registration_type="HOUSEHOLD")
        self.dep = reg_svc.add_dependant(
            self.m, relationship=SchemeDependant.Relationship.SPOUSE,
            name="Household Spouse", user=self.treasurer)

    def test_record_dependant_death_marks_died_on_and_deactivates(self):
        reg_svc.record_dependant_death(self.dep, died_on=TODAY, user=self.treasurer,
                                       reason="Passed away at home.")
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.died_on, TODAY)
        self.assertFalse(self.dep.active)
        self.assertEqual(self.dep.removed_on, TODAY)

    def test_cannot_record_death_twice(self):
        reg_svc.record_dependant_death(self.dep, died_on=TODAY, user=self.treasurer)
        with self.assertRaises(ValidationError):
            reg_svc.record_dependant_death(self.dep, died_on=TODAY, user=self.treasurer)

    def test_a_case_can_still_be_raised_for_a_deceased_dependant(self):
        reg_svc.record_dependant_death(self.dep, died_on=TODAY, user=self.treasurer)
        case = case_svc.create_case(
            self.scheme, event_type=self.event, membership=self.m, dependant=self.dep,
            event_date=TODAY, user=self.treasurer)
        self.assertEqual(case.dependant_id, self.dep.pk)

    def test_the_view_action_records_the_death_and_prompts_toward_a_case(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(
            reverse("benevolent_household", args=[self.m.pk]),
            {"deceased": self.dep.pk, "died_on": TODAY.isoformat(),
             "reason": "Passed away."})
        self.assertEqual(r.status_code, 302)
        self.dep.refresh_from_db()
        self.assertIsNotNone(self.dep.died_on)

    def test_the_household_page_shows_a_deceased_pill_and_case_link(self):
        reg_svc.record_dependant_death(self.dep, died_on=TODAY, user=self.treasurer)
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_membership_detail", args=[self.m.pk])).content.decode()
        self.assertIn("Deceased", body)
        self.assertIn(reverse("benevolent_case_new", args=[self.scheme.pk]), body)


# ===========================================================================
# 7. Contributions list: pagination (confirmed already present) + year selector
# ===========================================================================

class ContributionListYearSelectorTests(BugfixFixture):

    def test_the_page_paginates(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_contribution_list")).content.decode()
        self.assertIn("page_obj", "page_obj")  # sanity: response renders at all
        r = self.client.get(reverse("benevolent_contribution_list"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("page_obj", r.context)

    def test_defaults_to_the_current_year(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_contribution_list"))
        self.assertEqual(r.context["start"], dt.date(TODAY.year, 1, 1))

    def test_a_year_selector_is_offered(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_contribution_list")).content.decode()
        self.assertIn('name="year"', body)
        self.assertIn(f'value="{TODAY.year}"', body)

    def test_picking_a_year_scopes_to_that_whole_year(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_contribution_list"), {"year": "2024"})
        self.assertEqual(r.context["start"], dt.date(2024, 1, 1))
        self.assertEqual(r.context["end"], dt.date(2024, 12, 31))


# ===========================================================================
# 8. Bulk contribution import + discoverability
# ===========================================================================

class BulkContributionImportTests(BugfixFixture):

    def _csv(self, rows):
        header = ["member_name", "member_phone", "date", "amount", "kind",
                  "period_label", "channel", "note"]
        lines = [",".join(header)] + [",".join(r) for r in rows]
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile("contribs.csv", "\n".join(lines).encode(),
                                  content_type="text/csv")

    def test_a_contribution_row_posts_for_an_already_enrolled_member(self):
        mary = Member.objects.create(name="Contrib Import Member", phone="0722000555")
        m = reg_svc.register(self.scheme, mary, joined_on=TODAY - dt.timedelta(days=60),
                             user=self.treasurer)
        self.client.force_login(self.treasurer)
        f = self._csv([[
            "Contrib Import Member", "0722000555", TODAY.isoformat(), "100", "DUES",
            "2026-01", "CASH", "test note"]])
        r = self.client.post(
            reverse("benevolent_bulk_import_contributions", args=[self.scheme.pk]),
            {"file": f})
        self.assertEqual(r.status_code, 200)
        from benevolent.models import BenevolentContribution
        self.assertTrue(BenevolentContribution.objects.filter(membership=m).exists())

    def test_a_row_for_someone_not_enrolled_is_reported_not_silently_created(self):
        Member.objects.create(name="Not Enrolled Person", phone="0722000556")
        self.client.force_login(self.treasurer)
        f = self._csv([[
            "Not Enrolled Person", "0722000556", TODAY.isoformat(), "100", "DUES",
            "", "CASH", ""]])
        r = self.client.post(
            reverse("benevolent_bulk_import_contributions", args=[self.scheme.pk]),
            {"file": f})
        self.assertContains(r, "not enrolled", status_code=200)

    def test_the_csv_template_downloads(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(
            reverse("benevolent_bulk_import_contributions", args=[self.scheme.pk]),
            {"template": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"member_name", r.content)

    def test_a_finance_officer_can_import_contributions(self):
        officer = User.objects.create_user("finofficer_import", password="x")
        Profile.objects.get(name="Benevolent Finance Officer (default)").users.add(officer)
        self.client.force_login(officer)
        r = self.client.get(
            reverse("benevolent_bulk_import_contributions", args=[self.scheme.pk]))
        self.assertEqual(r.status_code, 200)

    def test_a_registration_officer_cannot_import_contributions(self):
        """Contributions are Finance Officer territory, not Registration —
        the two bulk-import screens are gated by their matching roles."""
        officer = User.objects.create_user("regofficer_contribimport", password="x")
        Profile.objects.get(
            name="Benevolent Registration Officer (default)").users.add(officer)
        self.client.force_login(officer)
        r = self.client.get(
            reverse("benevolent_bulk_import_contributions", args=[self.scheme.pk]))
        self.assertNotEqual(r.status_code, 200)


class BulkImportDiscoverabilityTests(BugfixFixture):

    def test_the_scheme_detail_page_links_to_both_import_screens(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_scheme_detail", args=[self.scheme.pk])).content.decode()
        self.assertIn(reverse("benevolent_bulk_import", args=[self.scheme.pk]), body)
        self.assertIn(
            reverse("benevolent_bulk_import_contributions", args=[self.scheme.pk]), body)


# ===========================================================================
# 9. Case-count-based inactivity — confirmed already built, now exposed
# ===========================================================================

class MissedCaseLeviesInactivityTests(BugfixFixture):
    """inactivity_missed_cases and missed_case_levies() already existed and
    were already wired into the standing engine — a real, working answer to
    "deactivate a member who hasn't contributed in the last N cases,"
    which matters most exactly where Edwin named it: a levy-funded scheme
    with no monthly dues, where time alone says nothing if there simply
    haven't been any recent cases. The gap was narrower than "missing
    feature": the field was never exposed on the policy form at all, so
    nobody could actually configure it. Confirmed end-to-end here, now
    that it's reachable."""

    def setUp(self):
        super().setUp()
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=450), user=self.treasurer)
        v2.contribution_mode = SchemePolicy.ContributionMode.PER_CASE_LEVY
        v2.levy_amount = Decimal("500")
        v2.inactivity_missed_cases = 3
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

    def test_the_field_is_now_on_the_policy_form(self):
        from benevolent.forms import PolicyForm
        group_fields = [f for _, fields in PolicyForm.GROUPS for f in fields]
        self.assertIn("inactivity_missed_cases", group_fields)

    def test_a_member_who_misses_three_consecutive_levies_is_marked_inactive(self):
        from benevolent.services import standing as standing_svc
        mary = Member.objects.create(name="Missed Levies Member", phone="0722000777")
        m = reg_svc.register(self.scheme, mary, joined_on=TODAY - dt.timedelta(days=400),
                             user=self.treasurer)
        for i in range(3):
            other = Member.objects.create(name=f"Someone Else X{i}", phone=f"07000001{i}0")
            other_m = reg_svc.register(self.scheme, other,
                                       joined_on=TODAY - dt.timedelta(days=400),
                                       user=self.treasurer)
            case = BenevolentCase.objects.create(
                scheme=self.scheme, membership=other_m, event_type=self.event,
                event_date=TODAY - dt.timedelta(days=(3 - i) * 30),
                reported_date=TODAY, raised_by=self.treasurer)
            case_svc.submit_case(case, user=self.treasurer)
            case_svc.assess_case(case, user=self.treasurer)
            case_svc.approve_case(case, amount=Decimal("10000"), user=self.treasurer,
                                  allow_self_approval=True)
        result = standing_svc.assess(m, as_of=TODAY)
        self.assertEqual(result.standing, "INACTIVE")
        self.assertIn("did not contribute", result.reason.lower())

    def test_a_member_who_pays_their_levies_stays_in_good_standing(self):
        from benevolent.services import standing as standing_svc
        mary = Member.objects.create(name="Paying Levies Member", phone="0722000778")
        m = reg_svc.register(self.scheme, mary, joined_on=TODAY - dt.timedelta(days=400),
                             user=self.treasurer)
        for i in range(3):
            other = Member.objects.create(name=f"Someone Else Y{i}", phone=f"07000002{i}0")
            other_m = reg_svc.register(self.scheme, other,
                                       joined_on=TODAY - dt.timedelta(days=400),
                                       user=self.treasurer)
            case = BenevolentCase.objects.create(
                scheme=self.scheme, membership=other_m, event_type=self.event,
                event_date=TODAY - dt.timedelta(days=(3 - i) * 30),
                reported_date=TODAY, raised_by=self.treasurer)
            case_svc.submit_case(case, user=self.treasurer)
            case_svc.assess_case(case, user=self.treasurer)
            case_svc.approve_case(case, amount=Decimal("10000"), user=self.treasurer,
                                  allow_self_approval=True)
            contrib_svc.record_contribution(
                self.scheme, date=case.event_date, amount=Decimal("500"),
                user=self.treasurer, membership=m, case=case, kind="LEVY")
        result = standing_svc.assess(m, as_of=TODAY)
        self.assertNotEqual(result.standing, "INACTIVE")

    def test_a_case_raised_for_the_member_themself_is_not_counted_as_a_miss(self):
        from benevolent.services import standing as standing_svc
        mary = Member.objects.create(name="Own Bereavement Member", phone="0722000779")
        m = reg_svc.register(self.scheme, mary, joined_on=TODAY - dt.timedelta(days=400),
                             user=self.treasurer)
        # three cases, all raised FOR this member — should never count against them
        for i in range(3):
            case = BenevolentCase.objects.create(
                scheme=self.scheme, membership=m, event_type=self.event,
                event_date=TODAY - dt.timedelta(days=(3 - i) * 30),
                reported_date=TODAY, raised_by=self.treasurer)
            case_svc.submit_case(case, user=self.treasurer)
            case_svc.assess_case(case, user=self.treasurer)
            case_svc.approve_case(case, amount=Decimal("10000"), user=self.treasurer,
                                  allow_self_approval=True)
        result = standing_svc.assess(m, as_of=TODAY)
        self.assertNotEqual(result.standing, "INACTIVE")


# ===========================================================================
# 10. Member directory report (item 9): members + dependants, active/inactive filter
# ===========================================================================

class MemberDirectoryReportTests(BugfixFixture):

    def setUp(self):
        super().setUp()
        from benevolent.models import SchemeDependant
        active_member = Member.objects.create(name="Directory Active", phone="0722000900")
        self.active_m = reg_svc.register(
            self.scheme, active_member, joined_on=TODAY - dt.timedelta(days=30),
            user=self.treasurer)
        reg_svc.add_dependant(
            self.active_m, relationship=SchemeDependant.Relationship.CHILD,
            name="Active's Child", user=self.treasurer)

        inactive_member = Member.objects.create(name="Directory Inactive", phone="0722000901")
        self.inactive_m = reg_svc.register(
            self.scheme, inactive_member, joined_on=TODAY - dt.timedelta(days=30),
            user=self.treasurer)
        reg_svc.suspend(self.inactive_m, user=self.treasurer, reason="Testing.")

    def test_the_report_shows_every_member_by_default(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("engine_report", args=["benevolent_member_directory_report"])
        ).content.decode()
        self.assertIn("DIRECTORY ACTIVE", body)
        self.assertIn("DIRECTORY INACTIVE", body)

    def test_dependants_appear_inline_with_the_member(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("engine_report", args=["benevolent_member_directory_report"])
        ).content.decode()
        self.assertIn("Active&#x27;s Child", body)

    def test_the_active_only_filter_excludes_suspended_members(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("engine_report", args=["benevolent_member_directory_report"]),
            {"active": "1"}).content.decode()
        self.assertIn("DIRECTORY ACTIVE", body)
        self.assertNotIn("DIRECTORY INACTIVE", body)

    def test_the_inactive_only_filter_excludes_active_members(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("engine_report", args=["benevolent_member_directory_report"]),
            {"active": "0"}).content.decode()
        self.assertIn("DIRECTORY INACTIVE", body)
        self.assertNotIn("DIRECTORY ACTIVE", body)

    def test_exports_to_csv_xlsx_and_pdf(self):
        self.client.force_login(self.treasurer)
        for fmt in ("csv", "xlsx", "pdf"):
            r = self.client.get(
                reverse("engine_report", args=["benevolent_member_directory_report"]),
                {"export": fmt})
            self.assertEqual(r.status_code, 200, fmt)
