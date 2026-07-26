"""A member can amend a request while it is still theirs to amend.

The model has always declared `MEMBER_EDITABLE = {DRAFT, INFO_NEEDED}` and
exposed `member_may_edit`, and the submit service enforced it — but nothing let a
member actually change anything. A draft could be saved and sent and withdrawn,
never corrected. Someone who mistyped an amount or picked the wrong dependant had
to withdraw the request and start again, which loses the reference, the thread
and any documents already attached.

The rule the tests exist to hold is *when* editing is allowed:

  * **Draft** — theirs, never sent. Editable.
  * **More information needed** — the office has handed it back and is waiting.
    Editable, because that is precisely the moment a member needs to change it.
  * **Submitted / under review** — with a reviewer. Refused: changing it
    underneath them would mean the office approving something other than what it
    read. The reply thread is the way to add something.
  * **Approved / declined / withdrawn** — decided. Refused, because editing
    would rewrite history.

Enforced in `portal_svc.update_request` rather than in the view, so posting
straight to the URL cannot get round it.
"""
import datetime as dt

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import UserProfile
from core import roles
from departments.models import Department
from members.models import Member

from .models import BenevolentScheme, SchemeDependant
from .models_portal import PortalRequest
from .services import portal as portal_svc
from .services import registry as reg_svc


class DraftEditingTestBase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_user("tess-edit", password="office-pass-1")
        self.officer.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-edit",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent Scheme", code="BEN", fund=self.fund,
            created_by=self.officer, status=BenevolentScheme.Status.ACTIVE)
        self.member = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.membership = reg_svc.register(
            self.scheme, self.member, joined_on=dt.date.today() - dt.timedelta(days=90))
        self.account = portal_svc.activate(portal_svc.invite(self.member))
        profile = UserProfile.for_user(self.account.user)
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])
        self.client = Client()
        self.client.force_login(self.account.user)

    def _draft(self, **kwargs):
        kwargs.setdefault("kind", PortalRequest.Kind.CORRECTION)
        kwargs.setdefault("subject", "Missing tithe")
        kwargs.setdefault("detail", "March 500 is not showing")
        kwargs.setdefault("payload", {"what": "March contribution"})
        return portal_svc.create_request(self.account, **kwargs)


class WhenAMemberMayEditTests(DraftEditingTestBase):

    def test_a_draft_can_be_amended(self):
        req = self._draft()
        portal_svc.update_request(req, actor=self.account.user,
                                  subject="Missing tithe (corrected)")
        req.refresh_from_db()
        self.assertEqual(req.subject, "Missing tithe (corrected)")

    def test_a_request_handed_back_for_more_information_can_be_amended(self):
        """The moment a member most needs to change something."""
        req = self._draft()
        req.status = PortalRequest.Status.INFO_NEEDED
        req.save(update_fields=["status"])
        portal_svc.update_request(req, actor=self.account.user, detail="It was 700")
        req.refresh_from_db()
        self.assertEqual(req.detail, "It was 700")

    def test_a_submitted_request_cannot_be_amended(self):
        req = self._draft()
        portal_svc.submit_request(req, actor=self.account.user)
        with self.assertRaises(ValidationError):
            portal_svc.update_request(req, actor=self.account.user, subject="Changed")

    def test_a_request_under_review_cannot_be_amended(self):
        req = self._draft()
        req.status = PortalRequest.Status.UNDER_REVIEW
        req.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            portal_svc.update_request(req, actor=self.account.user, subject="Changed")

    def test_a_decided_request_cannot_be_amended(self):
        for status in (PortalRequest.Status.APPROVED,
                       PortalRequest.Status.DECLINED,
                       PortalRequest.Status.WITHDRAWN):
            with self.subTest(status=status):
                req = self._draft()
                req.status = status
                req.save(update_fields=["status"])
                with self.assertRaises(ValidationError):
                    portal_svc.update_request(req, actor=self.account.user,
                                              subject="Changed")

    def test_a_refused_edit_changes_nothing(self):
        """A rejected amendment must not half-apply."""
        req = self._draft()
        portal_svc.submit_request(req, actor=self.account.user)
        before = req.subject
        with self.assertRaises(ValidationError):
            portal_svc.update_request(req, actor=self.account.user, subject="Changed")
        req.refresh_from_db()
        self.assertEqual(req.subject, before)


class WhatMayBeChangedTests(DraftEditingTestBase):

    def test_untouched_fields_are_left_alone(self):
        """Amending one thing must not blank the rest."""
        req = self._draft()
        portal_svc.update_request(req, actor=self.account.user, subject="New subject")
        req.refresh_from_db()
        self.assertEqual(req.detail, "March 500 is not showing")
        self.assertEqual(req.payload, {"what": "March contribution"})

    def test_the_payload_can_be_amended(self):
        req = self._draft()
        portal_svc.update_request(req, actor=self.account.user,
                                  payload={"what": "March contribution of 700"})
        req.refresh_from_db()
        self.assertEqual(req.payload["what"], "March contribution of 700")

    def test_a_dependant_can_be_cleared(self):
        """False means "there is no longer one"; None means "leave it alone"."""
        dependant = SchemeDependant.objects.create(
            membership=self.membership, name="Grace Momanyi",
            relationship=SchemeDependant.Relationship.CHILD, active=True)
        req = self._draft(kind=PortalRequest.Kind.DEATH,
                          dependant=dependant, event_date=dt.date.today(),
                          payload={})
        self.assertEqual(req.dependant_id, dependant.pk)
        portal_svc.update_request(req, actor=self.account.user,
                                  dependant=False, deceased_name="Someone else")
        req.refresh_from_db()
        self.assertIsNone(req.dependant_id)

    def test_the_reference_survives_an_edit(self):
        """Editing is not re-creating; the member keeps the number they quote."""
        req = self._draft()
        reference = req.reference
        portal_svc.update_request(req, actor=self.account.user, subject="Changed")
        req.refresh_from_db()
        self.assertEqual(req.reference, reference)

    def test_an_edit_can_submit_in_the_same_step(self):
        req = self._draft()
        portal_svc.update_request(req, actor=self.account.user,
                                  subject="Ready now", submit=True)
        req.refresh_from_db()
        self.assertEqual(req.status, PortalRequest.Status.SUBMITTED)
        self.assertIsNotNone(req.submitted_at)

    def test_an_invalid_amendment_is_refused(self):
        """The model's own rules still apply to an edit.

        A death report must say who died; blanking both the dependant and the
        name has to fail rather than save a report of nobody's death.
        """
        dependant = SchemeDependant.objects.create(
            membership=self.membership, name="Grace Momanyi",
            relationship=SchemeDependant.Relationship.CHILD, active=True)
        req = self._draft(kind=PortalRequest.Kind.DEATH, dependant=dependant,
                          event_date=dt.date.today(), payload={})
        with self.assertRaises(ValidationError):
            portal_svc.update_request(req, actor=self.account.user,
                                      dependant=False, deceased_name="")


class TheEditPageTests(DraftEditingTestBase):

    def test_the_detail_page_offers_editing_on_a_draft(self):
        req = self._draft()
        body = self.client.get(
            reverse("portal_request_detail", args=[req.pk])).content.decode()
        self.assertIn(reverse("portal_request_edit", args=[req.pk]), body)

    def test_the_detail_page_does_not_offer_editing_once_submitted(self):
        req = self._draft()
        portal_svc.submit_request(req, actor=self.account.user)
        body = self.client.get(
            reverse("portal_request_detail", args=[req.pk])).content.decode()
        self.assertNotIn(reverse("portal_request_edit", args=[req.pk]), body)

    def test_the_form_opens_filled_in(self):
        req = self._draft()
        body = self.client.get(
            reverse("portal_request_edit", args=[req.pk])).content.decode()
        self.assertIn('value="Missing tithe"', body)
        self.assertIn("March 500 is not showing", body)
        self.assertIn('value="March contribution"', body)

    def test_saving_the_form_keeps_it_a_draft(self):
        req = self._draft()
        self.client.post(reverse("portal_request_edit", args=[req.pk]),
                         {"subject": "Corrected", "detail": "It was 700",
                          "what": "March contribution of 700", "action": "draft"})
        req.refresh_from_db()
        self.assertEqual(req.subject, "Corrected")
        self.assertEqual(req.payload["what"], "March contribution of 700")
        self.assertEqual(req.status, PortalRequest.Status.DRAFT)

    def test_the_form_can_send_it_to_the_office(self):
        req = self._draft()
        self.client.post(reverse("portal_request_edit", args=[req.pk]),
                         {"subject": "Corrected", "detail": "It was 700",
                          "what": "x", "action": "submit"})
        req.refresh_from_db()
        self.assertEqual(req.status, PortalRequest.Status.SUBMITTED)

    def test_opening_the_edit_page_for_a_submitted_request_is_turned_away(self):
        req = self._draft()
        portal_svc.submit_request(req, actor=self.account.user)
        response = self.client.get(reverse("portal_request_edit", args=[req.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(str(req.pk), response["Location"])

    def test_posting_to_a_submitted_request_does_not_change_it(self):
        """The guard is in the service, so the URL cannot be used to get round it."""
        req = self._draft()
        portal_svc.submit_request(req, actor=self.account.user)
        self.client.post(reverse("portal_request_edit", args=[req.pk]),
                         {"subject": "Snuck in", "action": "draft"})
        req.refresh_from_db()
        self.assertEqual(req.subject, "Missing tithe")


class AnotherMembersDraftTests(DraftEditingTestBase):
    """Scope, checked on the edit path specifically.

    A new URL that takes a primary key is a new opportunity to reach someone
    else's record by guessing a number.
    """

    def setUp(self):
        super().setUp()
        other_member = Member.objects.create(name="Jane Nyamongo", phone="254700111222")
        reg_svc.register(self.scheme, other_member, joined_on=dt.date.today())
        self.other = portal_svc.activate(portal_svc.invite(other_member))
        self.their_draft = portal_svc.create_request(
            self.other, kind=PortalRequest.Kind.CORRECTION,
            subject="Their private business", payload={"what": "theirs"})

    def test_a_member_cannot_open_another_members_draft(self):
        response = self.client.get(
            reverse("portal_request_edit", args=[self.their_draft.pk]))
        self.assertIn(
            response.status_code, (403, 404),
            "Another member's draft opened for editing.")

    def test_a_member_cannot_amend_another_members_draft(self):
        self.client.post(
            reverse("portal_request_edit", args=[self.their_draft.pk]),
            {"subject": "Tampered", "action": "draft"})
        self.their_draft.refresh_from_db()
        self.assertEqual(self.their_draft.subject, "Their private business")

    def test_the_scope_check_is_what_refuses_it(self):
        """Not merely that nothing changed — that it was actively refused."""
        from benevolent.services.portal import scope
        with self.assertRaises(PermissionDenied):
            scope(self.account).request(self.their_draft.pk)
