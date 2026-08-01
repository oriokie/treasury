"""The member-facing case timeline had two defects in one expression, both of
which needed a member with a real case to surface — and until the portal was
exercised, no such data existed anywhere.

    ctx["timeline"] = case.events.filter(
        kind__in=["RAISED", "SUBMITTED", "ASSESSED", "APPROVED", "REJECTED",
                  "PAID", "CLOSED", "DOCUMENT"]).order_by("at")

  1. `order_by("at")` — CaseEvent has no `at` field (it has `on` and
     `created_at`), so opening your own case in the portal raised FieldError:
     a 500 on a member-facing page.
  2. "PAID" and "DOCUMENT" are not CaseEvent.Kind values. The real values are
     PAY_PAID and DOC_ADD, so the two events a claimant most wants to see —
     that they were paid, and that their documents were received — were
     silently filtered out. That one does not crash; it just quietly withholds
     the answer the member came for.

The filter now goes through the enum, so a wrong name fails loudly instead.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from benevolent.models import (BenevolentCase, BenevolentEventType,
                               BenevolentScheme, CaseEvent, SchemeMembership)
from benevolent.models_portal import MemberAccount
from core.roles import MEMBER, TREASURER
from departments.models import Department
from members.models import Member

TODAY = dt.date(2026, 6, 1)


class _PortalMember(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("pt_tr", password="x", is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        fund = Department.objects.create(name="PtFund", fund_type="LOCAL",
                                         category="MINISTRY")
        self.scheme = BenevolentScheme.objects.create(
            name="Pt Scheme", code="PT", fund=fund, created_by=self.tr,
            status=BenevolentScheme.Status.ACTIVE)
        self.et = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")

        self.member = Member.objects.create(name="PORTAL MEMBER")
        self.mem = SchemeMembership.objects.create(
            scheme=self.scheme, member=self.member, joined_on=dt.date(2026, 1, 1),
            status=SchemeMembership.Status.ACTIVE)
        self.user = User.objects.create_user("pt_member", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=MEMBER)[0])
        self.account = MemberAccount.objects.create(
            user=self.user, member=self.member,
            status=MemberAccount.Status.ACTIVE)

        self.case = BenevolentCase.objects.create(
            scheme=self.scheme, event_type=self.et, membership=self.mem,
            event_date=TODAY, status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("5000"), raised_by=self.tr)
        for kind in (CaseEvent.Kind.RAISED, CaseEvent.Kind.SUBMITTED,
                     CaseEvent.Kind.ASSESSED, CaseEvent.Kind.APPROVED,
                     CaseEvent.Kind.DOCUMENT_ADDED, CaseEvent.Kind.PAYOUT_PAID,
                     CaseEvent.Kind.COMMITTEE_VOTE, CaseEvent.Kind.NOTE):
            CaseEvent.objects.create(case=self.case, kind=kind, on=TODAY,
                                     summary=f"{kind} happened")
        self.client.force_login(self.user)

    def _timeline(self):
        r = self.client.get(f"/portal/cases/{self.case.pk}/")
        self.assertEqual(r.status_code, 200)
        return r, [e.kind for e in r.context["timeline"]]


class TimelineRendersTests(_PortalMember):
    def test_the_page_opens_at_all(self):
        """It 500'd with FieldError: Cannot resolve keyword 'at'."""
        self.assertEqual(
            self.client.get(f"/portal/cases/{self.case.pk}/").status_code, 200)

    def test_the_member_can_see_that_they_were_paid(self):
        """The whole point of the page, and previously missing: PAY_PAID was
        filtered out because the filter looked for "PAID"."""
        _, kinds = self._timeline()
        self.assertIn(CaseEvent.Kind.PAYOUT_PAID, kinds)

    def test_the_member_can_see_their_documents_were_received(self):
        _, kinds = self._timeline()
        self.assertIn(CaseEvent.Kind.DOCUMENT_ADDED, kinds)

    def test_progress_events_are_all_present(self):
        _, kinds = self._timeline()
        for k in (CaseEvent.Kind.RAISED, CaseEvent.Kind.SUBMITTED,
                  CaseEvent.Kind.ASSESSED, CaseEvent.Kind.APPROVED):
            self.assertIn(k, kinds)

    def test_the_committees_working_papers_stay_private(self):
        """A member sees progress, not deliberations."""
        _, kinds = self._timeline()
        self.assertNotIn(CaseEvent.Kind.COMMITTEE_VOTE, kinds)
        self.assertNotIn(CaseEvent.Kind.NOTE, kinds)

    def test_it_reads_oldest_first(self):
        """A progress timeline runs forwards; the model's own Meta.ordering is
        newest-first, which is right for an audit log and wrong here."""
        r, _ = self._timeline()
        tl = list(r.context["timeline"])
        self.assertEqual(tl, sorted(tl, key=lambda e: (e.on, e.created_at)))

    def test_every_kind_named_in_the_filter_is_a_real_kind(self):
        """The defect that did not crash. Guarding the values themselves means
        a renamed kind fails here rather than quietly vanishing from a
        member's timeline."""
        real = {k for k, _ in CaseEvent.Kind.choices}
        _, kinds = self._timeline()
        self.assertTrue(set(kinds) <= real)


class TimelineScopingTests(_PortalMember):
    def test_a_member_cannot_open_someone_elses_case(self):
        other_member = Member.objects.create(name="OTHER MEMBER")
        other_mem = SchemeMembership.objects.create(
            scheme=self.scheme, member=other_member, joined_on=dt.date(2026, 1, 1),
            status=SchemeMembership.Status.ACTIVE)
        other_case = BenevolentCase.objects.create(
            scheme=self.scheme, event_type=self.et, membership=other_mem,
            event_date=TODAY, status=BenevolentCase.Status.PAID,
            raised_by=self.tr)
        r = self.client.get(f"/portal/cases/{other_case.pk}/")
        self.assertIn(r.status_code, (403, 404),
                      "a member reached another member's case")

    def test_viewing_a_case_is_logged(self):
        from benevolent.models_portal import PortalAccessLog
        before = PortalAccessLog.objects.count()
        self.client.get(f"/portal/cases/{self.case.pk}/")
        self.assertGreater(PortalAccessLog.objects.count(), before)

    def test_a_member_cannot_download_someone_elses_document(self):
        """Only ever verifiable with a second account, and the demo data has
        exactly one — so on real data this guard had never been executed."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from benevolent.models import PortalDocument
        other_user = User.objects.create_user("pt_other", password="x")
        other_user.groups.add(Group.objects.get_or_create(name=MEMBER)[0])
        other_account = MemberAccount.objects.create(
            user=other_user, member=Member.objects.create(name="OTHER HOUSEHOLD"),
            status=MemberAccount.Status.ACTIVE)
        doc = PortalDocument.objects.create(
            account=other_account, kind="MEDICAL", label="Their invoice",
            file=SimpleUploadedFile("theirs.pdf", b"%PDF-1.4 not yours",
                                    content_type="application/pdf"))

        r = self.client.get(f"/portal/documents/{doc.pk}/download/")
        self.assertIn(r.status_code, (403, 404),
                      "a member downloaded another member's document")
