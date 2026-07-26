"""Correcting a dependant opens a form that already knows who is meant.

The household page has always linked "Correct" beside each person, carrying that
person's id in the query string. The form ignored it. The member arrived at a
blank form and had to retype the name, relationship, telephone number and date of
birth of someone whose details were on the page they had just left — and the
office then received four values with no indication which one was the correction.

The form now opens filled in from the church's own record, so the member changes
the field that is wrong and everything else stays as the church holds it. The
difference that matters is not the typing saved; it is that a correction now
arrives as a correction.
"""
import datetime as dt

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import UserProfile
from core import roles
from departments.models import Department
from members.models import Member

from .models import BenevolentScheme, SchemeDependant
from .services import portal as portal_svc
from .services import registry as reg_svc


class HouseholdCorrectionPrefillTests(TestCase):

    def setUp(self):
        self.officer = User.objects.create_user("tess-hh", password="office-pass-1")
        self.officer.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-hh",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent Scheme", code="BEN", fund=self.fund,
            created_by=self.officer, status=BenevolentScheme.Status.ACTIVE)
        self.member = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.membership = reg_svc.register(
            self.scheme, self.member, joined_on=dt.date.today() - dt.timedelta(days=90))
        self.dependant = SchemeDependant.objects.create(
            membership=self.membership, name="Grace Momanyi",
            relationship=SchemeDependant.Relationship.CHILD,
            phone="254711000111", date_of_birth=dt.date(2015, 4, 9), active=True)

        self.account = portal_svc.activate(portal_svc.invite(self.member))
        profile = UserProfile.for_user(self.account.user)
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])
        self.client = Client()
        self.client.force_login(self.account.user)

    def _form(self, query=""):
        url = reverse("portal_request_new", args=["HOUSEHOLD"]) + query
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _correction_form(self):
        return self._form(f"?dependant={self.dependant.pk}&op=update")

    # -- the link the page offers --------------------------------------------

    def test_the_household_page_links_the_person_being_corrected(self):
        body = self.client.get(reverse("portal_household")).content.decode()
        self.assertIn(f"dependant={self.dependant.pk}", body)

    # -- what the form knows -------------------------------------------------

    def test_the_person_is_already_chosen(self):
        self.assertIn(f'value="{self.dependant.pk}" selected', self._correction_form())

    def test_the_kind_of_change_is_already_chosen(self):
        self.assertIn('value="update" selected', self._correction_form())

    def test_their_current_name_is_filled_in(self):
        self.assertIn('value="Grace Momanyi"', self._correction_form())

    def test_their_current_relationship_is_selected(self):
        self.assertIn('value="CHILD" selected', self._correction_form())

    def test_their_current_phone_is_filled_in(self):
        self.assertIn('value="254711000111"', self._correction_form())

    def test_their_date_of_birth_is_filled_in(self):
        self.assertIn('value="2015-04-09"', self._correction_form())

    def test_the_subject_names_the_person(self):
        """So the office sees whose record it is without opening the payload."""
        self.assertIn("Grace Momanyi", self._correction_form())

    def test_a_removal_request_is_marked_as_one(self):
        body = self._form(f"?dependant={self.dependant.pk}&op=remove")
        self.assertIn('value="remove" selected', body)
        self.assertIn(f'value="{self.dependant.pk}" selected', body)

    def test_a_dependant_with_no_phone_or_birthday_is_left_blank_not_broken(self):
        sparse = SchemeDependant.objects.create(
            membership=self.membership, name="Silas Momanyi",
            relationship=SchemeDependant.Relationship.PARENT, active=True)
        body = self._form(f"?dependant={sparse.pk}&op=update")
        self.assertIn('value="Silas Momanyi"', body)
        self.assertIn(f'value="{sparse.pk}" selected', body)

    def test_a_dependant_on_the_church_roll_shows_the_name_the_church_holds(self):
        """That is the name being corrected, so it is the one to show."""
        roll_member = Member.objects.create(name="Joan Momanyi", phone="254722000222")
        linked = SchemeDependant.objects.create(
            membership=self.membership, member=roll_member,
            relationship=SchemeDependant.Relationship.CHILD, active=True)
        body = self._form(f"?dependant={linked.pk}&op=update")
        self.assertIn(f'value="{linked.display_name}"', body)

    # -- it must not get in the way ------------------------------------------

    def test_the_plain_form_is_still_blank(self):
        body = self._form()
        self.assertNotIn('value="Grace Momanyi"', body)
        self.assertNotIn('value="254711000111"', body)

    def test_an_unknown_person_opens_an_ordinary_blank_form(self):
        """A stale or mistyped link is a nuisance, not an error page."""
        body = self._form("?dependant=999999&op=update")
        self.assertNotIn('value="Grace Momanyi"', body)

    def test_a_nonsense_id_opens_an_ordinary_blank_form(self):
        body = self._form("?dependant=notanumber&op=update")
        self.assertNotIn('value="Grace Momanyi"', body)

    def test_a_nonsense_operation_is_ignored(self):
        body = self._form(f"?dependant={self.dependant.pk}&op=explode")
        self.assertNotIn('value="explode"', body)

    def test_another_members_dependant_is_not_disclosed(self):
        """The query string must not become a way to read someone else's household."""
        other = Member.objects.create(name="Jane Nyamongo", phone="254700111222")
        other_membership = reg_svc.register(self.scheme, other,
                                            joined_on=dt.date.today())
        theirs = SchemeDependant.objects.create(
            membership=other_membership, name="Private Person",
            relationship=SchemeDependant.Relationship.SPOUSE,
            phone="254799888777", active=True)
        body = self._form(f"?dependant={theirs.pk}&op=update")
        self.assertNotIn("Private Person", body)
        self.assertNotIn("254799888777", body)

    def test_prefilling_does_not_leak_into_other_request_kinds(self):
        body = self.client.get(
            reverse("portal_request_new", args=["ASSISTANCE"])
            + f"?dependant={self.dependant.pk}&op=update").content.decode()
        self.assertNotIn('value="254711000111"', body)

    # -- and it still submits ------------------------------------------------

    def test_the_prefilled_form_submits_as_a_correction(self):
        from .models_portal import PortalRequest
        self.client.post(
            reverse("portal_request_new", args=["HOUSEHOLD"]),
            {"subject": "Correction to Grace Momanyi's details",
             "op": "update", "dependant": self.dependant.pk,
             "relationship": SchemeDependant.Relationship.CHILD,
             "name": "Grace Momanyi", "phone": "254711000999",
             "date_of_birth": "2015-04-09",
             "detail": "Her phone number changed.", "action": "submit"})
        req = PortalRequest.objects.filter(account=self.account).latest("id")
        self.assertEqual(req.kind, PortalRequest.Kind.HOUSEHOLD)
        self.assertEqual(req.dependant_id, self.dependant.pk)
        self.assertEqual(req.payload["op"], "update")
        self.assertEqual(req.payload["phone"], "254711000999")
        self.assertEqual(req.status, PortalRequest.Status.SUBMITTED)
