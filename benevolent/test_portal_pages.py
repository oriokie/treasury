"""Every portal page renders, and the workflows land where they should.

The security tests next door pin the rules. These pin the *surface*: a template
that raises on an empty queryset, a URL that reverses to nothing, an approval
that quietly fails to call the service it claims to call. All cheap to break and
none of it caught by a model test.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from departments.models import Department
from members.models import Member

from .models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                     PortalRequest, SchemeDependant, SchemeMembership)
from .services import portal as portal_svc
from .services import registry as reg_svc


class PortalPageTestBase(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.treasurer = User.objects.create_user("tess", password="office-pass-1")
        self.treasurer.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent Scheme", code="BEN", fund=self.fund,
            created_by=self.treasurer, status=BenevolentScheme.Status.ACTIVE)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER",
            covers_dependants=True)

        self.ruth = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.membership = reg_svc.register(
            self.scheme, self.ruth, joined_on=dt.date(2024, 1, 1))
        if self.membership.status != SchemeMembership.Status.ACTIVE:
            reg_svc.admit(self.membership, notify=False)
            self.membership.refresh_from_db()

        self.account = portal_svc.activate(portal_svc.invite(self.ruth))
        self.account.user.set_password("portal-pass-123")
        self.account.user.save()
        from accounts.models import UserProfile
        profile = UserProfile.for_user(self.account.user)
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])

        self.client = Client()
        self.client.login(username=self.account.user.username,
                          password="portal-pass-123")


class PortalPageRenderTests(PortalPageTestBase):
    """Every member-facing page returns 200 for a member with an empty record.

    The empty case is the one that breaks: a member invited today has no
    contributions, no cases and no documents, and that is exactly the first
    thing they will look at.
    """

    def test_every_portal_page_renders(self):
        for name in ["portal_home", "portal_contributions", "portal_statement",
                     "portal_standing", "portal_household", "portal_cases",
                     "portal_requests", "portal_documents",
                     "portal_notifications", "portal_profile"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200,
                                 f"{name} did not render")

    def test_every_request_form_renders(self):
        for kind in PortalRequest.Kind.values:
            with self.subTest(kind=kind):
                response = self.client.get(
                    reverse("portal_request_new", args=[kind]))
                self.assertEqual(response.status_code, 200)

    def test_an_unknown_request_kind_is_a_404_not_a_crash(self):
        response = self.client.get(reverse("portal_request_new", args=["NONSENSE"]))
        self.assertEqual(response.status_code, 404)

    def test_the_navigation_shows_the_member_branch_only(self):
        response = self.client.get(reverse("portal_home"))
        body = response.content.decode()
        self.assertIn("My contributions", body)
        self.assertNotIn("Executive overview", body,
                         "A member must not be shown office navigation.")

    def test_statement_exports_reuse_the_office_formatter(self):
        for fmt, expected in [("csv", "text/csv"),
                              ("xlsx", "application/vnd.openxmlformats")]:
            with self.subTest(fmt=fmt):
                response = self.client.get(
                    reverse("portal_statement") + f"?export={fmt}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, response["Content-Type"])


class PortalSubmissionTests(PortalPageTestBase):
    """Submitting through the form produces the same thing the service does."""

    def test_a_member_can_submit_an_assistance_request(self):
        response = self.client.post(
            reverse("portal_request_new", args=["ASSISTANCE"]),
            {"subject": "Help with hospital bill",
             "detail": "My son was admitted last week.",
             "event_type": self.bereavement.pk,
             "event_date": dt.date.today().isoformat(),
             "action": "submit"})
        self.assertEqual(response.status_code, 302)
        req = PortalRequest.objects.get(account=self.account)
        self.assertEqual(req.status, PortalRequest.Status.SUBMITTED)
        self.assertEqual(req.kind, PortalRequest.Kind.ASSISTANCE)

    def test_a_member_cannot_submit_against_another_members_dependant(self):
        """The form's own ids are not trusted — a posted dependant belonging to
        someone else is dropped, not accepted."""
        other = Member.objects.create(name="Kevin Ogega", phone="254716804186")
        other_membership = reg_svc.register(
            self.scheme, other, joined_on=dt.date(2024, 1, 1))
        if other_membership.status != SchemeMembership.Status.ACTIVE:
            reg_svc.admit(other_membership, notify=False)
        other_dependant = reg_svc.add_dependant(
            other_membership, relationship=SchemeDependant.Relationship.CHILD,
            name="Their child")

        self.client.post(
            reverse("portal_request_new", args=["DEATH"]),
            {"subject": "Bereavement", "dependant": other_dependant.pk,
             "deceased_name": "Someone", "event_date": dt.date.today().isoformat(),
             "action": "submit"})
        req = PortalRequest.objects.get(account=self.account)
        self.assertIsNone(req.dependant_id,
                          "A dependant belonging to another member was accepted.")

    def test_a_member_cannot_open_another_members_request(self):
        other = Member.objects.create(name="Kevin Ogega", phone="254716804186")
        other_account = portal_svc.activate(portal_svc.invite(other))
        their_request = portal_svc.create_request(
            other_account, kind=PortalRequest.Kind.CORRECTION, subject="private")
        response = self.client.get(
            reverse("portal_request_detail", args=[their_request.pk]))
        self.assertEqual(response.status_code, 403)


class PortalApprovalIntegrationTests(PortalPageTestBase):
    """Approval delegates to the service that owns the change."""

    def setUp(self):
        super().setUp()
        self.office = Client()
        self.office.login(username="tess", password="office-pass-1")

    def test_approving_a_household_request_calls_the_registry(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.HOUSEHOLD,
            subject="Add my daughter",
            payload={"op": "add", "relationship": "CHILD",
                     "name": "Faith Momanyi", "date_of_birth": "2015-04-02"},
            submit=True)
        portal_svc.approve_request(req, user=self.treasurer)

        dependant = SchemeDependant.objects.filter(
            membership=self.membership, name="Faith Momanyi").first()
        self.assertIsNotNone(dependant, "The registry service was not called.")
        # registry.add_dependant sets the coverage date itself — the portal
        # must not have been able to backdate it
        self.assertEqual(dependant.registered_on, dt.date.today())

    def test_approving_an_assistance_request_raises_a_draft_case_only(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.ASSISTANCE,
            subject="Bereavement help", event_type=self.bereavement,
            event_date=dt.date.today(), submit=True)
        portal_svc.approve_request(req, user=self.treasurer)
        req.refresh_from_db()

        self.assertIsNotNone(req.case, "No case was raised.")
        self.assertEqual(req.case.status, BenevolentCase.Status.DRAFT,
                         "Approving a portal request must not skip assessment.")
        self.assertIsNone(req.case.approved_amount,
                          "A portal approval must not approve the money.")

    def test_approving_a_profile_request_updates_the_roll(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.PROFILE,
            subject="Correct my name",
            payload={"name": "Ruth Momanyi Nyaboke"}, submit=True)
        portal_svc.approve_request(req, user=self.treasurer)
        self.ruth.refresh_from_db()
        # The roll stores names uppercased, for collation-independent matching
        # against bank statements and envelopes (members.Member.save). The
        # portal gets that for free precisely because it saves through the
        # model rather than writing the column — a portal that did its own
        # UPDATE would have quietly created a name the matcher cannot find.
        self.assertEqual(self.ruth.name, "RUTH MOMANYI NYABOKE")

    def test_the_office_queue_and_review_pages_render(self):
        portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.CORRECTION,
            subject="Missing contribution", submit=True)
        req = PortalRequest.objects.get(account=self.account)
        for url in [reverse("portal_admin_accounts"),
                    reverse("portal_admin_queue"),
                    reverse("portal_admin_review", args=[req.pk])]:
            with self.subTest(url=url):
                self.assertEqual(self.office.get(url).status_code, 200)

    def test_a_member_cannot_reach_the_office_review_screens(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.CORRECTION, subject="x",
            submit=True)
        for url in [reverse("portal_admin_queue"),
                    reverse("portal_admin_review", args=[req.pk])]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/portal/"))

    def test_deciding_needs_the_right_that_owns_the_change(self):
        """A login that may open the queue but holds neither the case nor the
        registration right cannot decide — the portal is not a way round the
        module's own separation of duties."""
        clerk = User.objects.create_user("clerk", password="x")
        clerk.groups.add(Group.objects.get_or_create(name=roles.ASSISTANT)[0])
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.HOUSEHOLD,
            subject="Add someone",
            payload={"op": "add", "relationship": "CHILD", "name": "A Child"},
            submit=True)

        from .views_portal_admin import _may_decide
        # an assistant satisfies can_manage_benevolent, so this one may decide;
        # the auditor below is the one who may look and not touch
        self.assertTrue(_may_decide(clerk, req))

        auditor = User.objects.create_user("aud", password="x")
        auditor.groups.add(Group.objects.get_or_create(name=roles.AUDITOR)[0])
        self.assertFalse(_may_decide(auditor, req))


class PortalDocumentTests(PortalPageTestBase):
    def test_uploads_are_limited_to_supported_types(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import PortalDocument

        bad = SimpleUploadedFile("payload.exe", b"MZ...", content_type="application/x-msdownload")
        self.client.post(reverse("portal_documents"), {"documents": bad})
        self.assertEqual(PortalDocument.objects.count(), 0,
                         "An unsupported file type was accepted.")

        good = SimpleUploadedFile("permit.pdf", b"%PDF-1.4 ...",
                                  content_type="application/pdf")
        self.client.post(reverse("portal_documents"),
                         {"documents": good, "document_kind": "BURIAL_PERMIT"})
        self.assertEqual(PortalDocument.objects.count(), 1)

    def test_a_document_attached_to_a_case_can_no_longer_be_withdrawn(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import PortalDocument

        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.ASSISTANCE,
            subject="Help", event_type=self.bereavement,
            event_date=dt.date.today(), submit=True)
        doc = PortalDocument.objects.create(
            account=self.account, request=req,
            kind=PortalDocument.Kind.BURIAL_PERMIT,
            file=SimpleUploadedFile("permit.pdf", b"%PDF-1.4"),
            original_name="permit.pdf")

        portal_svc.approve_request(req, user=self.treasurer)
        doc.refresh_from_db()
        self.assertIsNotNone(doc.attachment,
                             "The document was not mirrored onto the case.")
        self.assertFalse(doc.may_withdraw,
                         "Evidence behind a decision must stay on the record.")


class PortalInvitationJourneyTests(PortalPageTestBase):
    """The whole journey, from the office clicking Invite to the member seeing
    their record.

    Written after shipping a version in which it did not work. Each step of the
    invitation was individually correct and tested; nobody had walked them in
    order, and the join between two of them was a closed loop — the member set a
    password, signed in, and was told to set a password. A test per step cannot
    catch that. This one starts where the officer starts and stops where the
    member stops.
    """

    def setUp(self):
        super().setUp()
        # a second member who has NOT been invited, to walk from the beginning
        self.kevin = Member.objects.create(name="Kevin Ogega", phone="254716804186")
        self.kevin_membership = reg_svc.register(
            self.scheme, self.kevin, joined_on=dt.date(2024, 6, 1))
        if self.kevin_membership.status != SchemeMembership.Status.ACTIVE:
            reg_svc.admit(self.kevin_membership, notify=False)

    def test_an_invited_member_can_actually_get_in(self):
        from .models import MemberAccount

        # 1. the office invites them
        account = portal_svc.invite(self.kevin, actor=self.treasurer)
        self.assertEqual(account.status, MemberAccount.Status.INVITED)
        self.assertFalse(account.user.has_usable_password(),
                         "The office must never know the member's password.")

        # 2. the member sets a password through the ordinary reset flow
        # (setting the password also clears must_change_password — a pre_save
        #  signal on User does it, so no step of this is manual)
        account.user.set_password("chosen-by-the-member-1")
        account.user.save()

        # 3. they sign in
        client = Client()
        self.assertTrue(
            client.login(username=account.user.username,
                         password="chosen-by-the-member-1"),
            "The member could not authenticate at all.")

        # 4. ...and land on their own portal, not on a dead end
        response = client.get(reverse("after_login"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], reverse("portal_home"),
                         f"Signing in sent the member to "
                         f"{response.redirect_chain[-1][0]}, not the portal.")

        # 5. and the account is now ACTIVE, without anyone in the office acting
        account.refresh_from_db()
        self.assertEqual(account.status, MemberAccount.Status.ACTIVE)
        self.assertIsNotNone(account.activated_at)

    def test_signing_in_never_revives_a_withdrawn_account(self):
        """Activation must not be able to undo an officer's decision."""
        from .models import MemberAccount

        account = portal_svc.invite(self.kevin, actor=self.treasurer)
        account.user.set_password("chosen-by-the-member-1")
        account.user.save()
        portal_svc.suspend(account, actor=self.treasurer, reason="Under review")

        client = Client()
        client.login(username=account.user.username,
                     password="chosen-by-the-member-1")
        account.refresh_from_db()
        self.assertEqual(account.status, MemberAccount.Status.SUSPENDED,
                         "Signing in reactivated a suspended account.")

        response = client.get(reverse("portal_home"), follow=True)
        self.assertEqual(response.redirect_chain[-1][0],
                         reverse("portal_unavailable"))

    def test_the_unavailable_page_explains_the_actual_state(self):
        account = portal_svc.invite(self.kevin, actor=self.treasurer)
        portal_svc.suspend(account, actor=self.treasurer, reason="Under review")
        account.user.set_password("chosen-by-the-member-1")
        account.user.save()

        client = Client()
        client.login(username=account.user.username,
                     password="chosen-by-the-member-1")
        body = client.get(reverse("portal_unavailable")).content.decode()
        self.assertIn("suspended", body.lower())
        self.assertIn("Under review", body,
                      "A member told they are suspended should be told why.")


class PortalPagesWithRealDataTests(PortalPageTestBase):
    """The pages, rendered for a member who actually has something on them.

    `PortalPageRenderTests` renders every page for a member with an empty
    record, which is the right first check and was not enough. A member with no
    contributions has no dues schedule, so the schedule table never rendered and
    a template that referenced keys the service does not produce passed as
    green. On real data the standing page returned 500 — for every member, since
    every real member has a schedule.

    The specific trap is worth naming: Django resolves filter *arguments*
    eagerly, so `{{ a|default:b }}` raises when `b` cannot be resolved even
    though `a` is present and the default is never used. A missing key in a
    plain `{{ }}` renders blank; the same key inside a filter argument takes the
    page down.
    """

    def setUp(self):
        super().setUp()
        from .models import SchemePolicy
        from .services import schemes as scheme_svc

        policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=dt.date.today() - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=30,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY)
        scheme_svc.publish_policy(policy, user=self.treasurer)

    def test_the_standing_page_renders_with_a_real_dues_schedule(self):
        response = self.client.get(reverse("portal_standing"))
        self.assertEqual(response.status_code, 200,
                         "The standing page failed for a member with a schedule.")

    def test_the_dues_schedule_actually_appears(self):
        """Guards the fix rather than just the absence of a crash: the page
        must show the periods, not silently render an empty table."""
        from .services import contributions as contrib_svc
        schedule = contrib_svc.dues_schedule(self.membership,
                                             self.scheme.current_policy)
        if not schedule:
            self.skipTest("No schedule produced for this policy shape.")
        body = self.client.get(reverse("portal_standing")).content.decode()
        self.assertIn(str(schedule[-1]["period"]), body,
                      "The schedule was computed but not rendered.")

    def test_household_renders_for_a_dependant_with_no_linked_member(self):
        """A dependant recorded by name only — the common case, since most
        family members are not themselves on the church roll."""
        from .services import registry as reg_svc
        reg_svc.add_dependant(
            self.membership, relationship=SchemeDependant.Relationship.CHILD,
            name="Faith Momanyi")
        for name in ["portal_household", "portal_request_new"]:
            url = (reverse(name, args=["HOUSEHOLD"]) if name == "portal_request_new"
                   else reverse(name))
            self.assertEqual(self.client.get(url).status_code, 200,
                             f"{name} failed for a name-only dependant.")


class PortalPagesDoNotLeakTemplateMarkupTests(PortalPagesWithRealDataTests):
    """No portal page shows a member raw template markup.

    Two of the five templates that shipped with unrendered multi-line comments
    in v3.19.2 were portal templates — `portal/_base.html`, which every portal
    page extends, and `portal/standing.html`. Neither the portal suites nor the
    seeded smoke test caught it: the portal suites assert status codes, and the
    smoke test cannot reach these pages at all, because they need a signed-in
    member and the confinement middleware turns an office login away.

    So the check has to live here. It is the same assertion
    `core.test_seeded_smoke.NoTemplateSyntaxLeaksIntoPagesTests` makes, applied
    to the pages that suite is structurally unable to see.

    **It extends the real-data fixture deliberately, and that is not incidental.**
    The first version of this class inherited the empty-record base, and it
    passed against a deliberately reintroduced leak — because the comment in
    `standing.html` sits inside `{% for %}` over the dues schedule, and a member
    with no contributions has no schedule, so the loop body never executed. A
    leak check that renders no loops checks almost nothing. Same lesson as
    #121/#122/#125, arrived at while writing the guard for #130.
    """

    ALLOWED_CONTEXT = ("${", "javascript:", "<script", "<code", "<pre")

    def _leaks(self, html):
        import re
        found = []
        for pattern in (r"\{#", r"\{%\s*\w", r"\{\{\s*\w"):
            for m in re.finditer(pattern, html):
                window = html[max(0, m.start() - 200):m.start()]
                if any(t in window.lower() for t in self.ALLOWED_CONTEXT):
                    continue
                found.append(html[m.start():m.start() + 60].replace("\n", " "))
        return found

    def test_no_portal_page_leaks_template_markup(self):
        failures = []
        pages = ["portal_home", "portal_contributions", "portal_statement",
                 "portal_standing", "portal_household", "portal_cases",
                 "portal_requests", "portal_documents", "portal_notifications",
                 "portal_profile"]
        for name in pages:
            response = self.client.get(reverse(name))
            if response.status_code != 200:
                continue
            if "text/html" not in response.headers.get("Content-Type", ""):
                continue
            leaks = self._leaks(response.content.decode("utf-8", "ignore"))
            if leaks:
                failures.append(f"  {name}: {leaks[:2]}")

        for kind in PortalRequest.Kind.values:
            response = self.client.get(reverse("portal_request_new", args=[kind]))
            if response.status_code != 200:
                continue
            leaks = self._leaks(response.content.decode("utf-8", "ignore"))
            if leaks:
                failures.append(f"  portal_request_new/{kind}: {leaks[:2]}")

        self.assertFalse(
            failures,
            "Portal pages showing a member raw template markup. The usual "
            "cause is a `{# ... #}` comment spanning several lines — Django's "
            "is single-line, so a multi-line one is not a comment and renders "
            "in full:\\n" + "\\n".join(failures))
