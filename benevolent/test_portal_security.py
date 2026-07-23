"""The member portal's security foundation.

These are the tests that matter most in this module. A portal bug that shows
one family another family's business is not a defect that gets noticed in use —
the person harmed by it is the one person who never sees it. So the object-level
rule, the confinement of a member login to the portal, and the refusal to give
one session two identities are all pinned here rather than trusted to review.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from members.models import Member

from .models import (BenevolentScheme, MemberAccount, PortalRequest, SchemeMembership)
from .services import portal as portal_svc


def _scheme(code="BEN", name="Benevolent Fund"):
    from departments.models import Department
    fund = Department.objects.create(
        name=f"{name} Fund", slug=f"{code.lower()}-fund",
        fund_type=Department.FundType.LOCAL,
        category=Department.Category.MINISTRY)
    return BenevolentScheme.objects.create(
        code=code, name=name, fund=fund,
        status=BenevolentScheme.Status.ACTIVE)


def _enrol(scheme, member):
    """Enrol through the registry service, not a raw create.

    Deliberate: these tests should exercise the same enrolment path production
    uses, so a membership built here carries the same numbering, events and
    invariants a real one does.
    """
    from .services import registry as reg_svc
    membership = reg_svc.register(scheme, member, joined_on=dt.date(2024, 1, 1))
    if membership.status != SchemeMembership.Status.ACTIVE:
        reg_svc.admit(membership, notify=False)
        membership.refresh_from_db()
    return membership


class PortalFoundationTests(TestCase):
    """Identity binding: the thing everything else derives from."""

    def setUp(self):
        self.scheme = _scheme()
        self.ruth = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.kevin = Member.objects.create(name="Kevin Ogega", phone="254716804186")

    def test_invite_creates_a_bound_login_with_no_usable_password(self):
        account = portal_svc.invite(self.ruth)
        self.assertEqual(account.member, self.ruth)
        self.assertEqual(account.status, MemberAccount.Status.INVITED)
        self.assertFalse(account.user.has_usable_password(),
                         "An invited account must not ship with a password.")
        self.assertIn(roles.MEMBER, set(account.user.groups.values_list("name", flat=True)))

    def test_invited_account_is_not_yet_a_portal_member(self):
        """INVITED is not ACTIVE. Until the member sets a password and accepts,
        the binding exists but grants nothing."""
        account = portal_svc.invite(self.ruth)
        self.assertFalse(roles.is_portal_member(account.user))
        portal_svc.activate(account)
        account.refresh_from_db()
        self.assertTrue(roles.is_portal_member(account.user))

    def test_a_member_cannot_be_invited_twice(self):
        portal_svc.invite(self.ruth)
        with self.assertRaises(ValidationError):
            portal_svc.invite(self.ruth)

    def test_an_office_login_cannot_be_turned_into_a_member_login(self):
        """One session, one identity. A treasurer who also holds a MemberAccount
        would make every object-level rule in this module ambiguous."""
        treasurer = User.objects.create_user("tess", password="x")
        treasurer.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        with self.assertRaises(ValidationError):
            portal_svc.invite(self.ruth, user=treasurer)

    def test_superuser_is_never_a_portal_member(self):
        admin = User.objects.create_superuser("root", "r@x.test", "x")
        admin.groups.add(Group.objects.get_or_create(name=roles.MEMBER)[0])
        self.assertFalse(roles.is_portal_member(admin))
        self.assertFalse(roles.is_portal_only(admin))

    def test_suspending_the_account_revokes_access_but_not_membership(self):
        membership = _enrol(self.scheme, self.ruth)
        account = portal_svc.activate(portal_svc.invite(self.ruth))
        portal_svc.suspend(account, reason="Under investigation")
        account.refresh_from_db()

        self.assertFalse(roles.is_portal_member(account.user))
        # still confined — a suspended member must not fall through to the office
        self.assertTrue(roles.is_portal_only(account.user))
        membership.refresh_from_db()
        self.assertEqual(membership.status, SchemeMembership.Status.ACTIVE,
                         "Losing a login must not lose cover.")

    def test_closing_deactivates_the_login_but_keeps_the_record(self):
        account = portal_svc.activate(portal_svc.invite(self.ruth))
        portal_svc.close(account, reason="Left the church")
        account.refresh_from_db()
        self.assertFalse(account.user.is_active)
        self.assertTrue(MemberAccount.objects.filter(pk=account.pk).exists(),
                        "The account row survives so the access log still resolves.")


class PortalScopeTests(TestCase):
    """The object-level rule. One member, one member's rows."""

    def setUp(self):
        self.scheme = _scheme()
        self.ruth = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.kevin = Member.objects.create(name="Kevin Ogega", phone="254716804186")
        self.ruth_m = _enrol(self.scheme, self.ruth)
        self.kevin_m = _enrol(self.scheme, self.kevin)
        self.ruth_acct = portal_svc.activate(portal_svc.invite(self.ruth))
        self.kevin_acct = portal_svc.activate(portal_svc.invite(self.kevin))

    def test_memberships_are_scoped_to_the_member(self):
        sc = portal_svc.scope(self.ruth_acct)
        self.assertEqual(list(sc.memberships()), [self.ruth_m])

    def test_fetching_another_members_enrolment_is_refused_not_empty(self):
        """A refusal, not a silent miss: an object-level check that returns
        None invites a view to fall through to something worse."""
        sc = portal_svc.scope(self.ruth_acct)
        with self.assertRaises(PermissionDenied):
            sc.membership(self.kevin_m.pk)

    def test_dependants_cases_and_requests_are_all_scoped(self):
        sc_ruth = portal_svc.scope(self.ruth_acct)
        sc_kevin = portal_svc.scope(self.kevin_acct)

        req = portal_svc.create_request(
            self.kevin_acct, kind=PortalRequest.Kind.CORRECTION,
            subject="My March contribution is missing")

        self.assertIn(req, sc_kevin.requests())
        self.assertNotIn(req, sc_ruth.requests())
        with self.assertRaises(PermissionDenied):
            sc_ruth.request(req.pk)

    def test_scope_refuses_to_be_built_without_an_account(self):
        with self.assertRaises(PermissionDenied):
            portal_svc.scope(None)


class PortalConfinementTests(TestCase):
    """A member login reaches the portal and nothing else."""

    def setUp(self):
        self.scheme = _scheme()
        self.ruth = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        _enrol(self.scheme, self.ruth)
        self.account = portal_svc.activate(portal_svc.invite(self.ruth))
        self.account.user.set_password("portal-pass-123")
        self.account.user.save()
        # the invite sets must_change_password; clear it so the confinement
        # middleware, not the password gate, is what this test observes
        from accounts.models import UserProfile
        profile = UserProfile.for_user(self.account.user)
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])

        self.client = Client()
        self.client.login(username=self.account.user.username,
                          password="portal-pass-123")

    def test_office_pages_redirect_to_the_portal(self):
        for name in ["dashboard", "benevolent_dashboard", "report_index"]:
            try:
                url = reverse(name)
            except Exception:
                continue
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302,
                             f"{name} should redirect a portal member away")
            self.assertTrue(response.url.startswith("/portal/"),
                            f"{name} sent a member to {response.url}, not the portal")

    def test_the_portal_itself_is_reachable(self):
        response = self.client.get(reverse("portal_home"))
        self.assertEqual(response.status_code, 200)

    def test_signing_out_is_still_reachable(self):
        response = self.client.post(reverse("logout"))
        self.assertIn(response.status_code, (200, 302))

    def test_an_office_login_is_not_confined(self):
        staff = User.objects.create_user("tess", password="x")
        staff.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        client = Client()
        client.login(username="tess", password="x")
        response = client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)


class PortalRequestLifecycleTests(TestCase):
    """Requests are claims, never changes."""

    def setUp(self):
        self.scheme = _scheme()
        self.ruth = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.membership = _enrol(self.scheme, self.ruth)
        self.account = portal_svc.activate(portal_svc.invite(self.ruth))
        self.officer = User.objects.create_user("officer", password="x")
        self.officer.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])

    def test_a_new_request_is_a_draft_with_a_reference(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.ASSISTANCE,
            subject="Help with school fees")
        self.assertEqual(req.status, PortalRequest.Status.DRAFT)
        self.assertTrue(req.reference.startswith("REQ-"))
        self.assertEqual(req.membership, self.membership,
                         "A member in one scheme should not have to choose it.")

    def test_references_do_not_collide_with_case_numbers(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.CORRECTION, subject="x")
        self.assertNotIn("BC-", req.reference)

    def test_submitting_hands_it_over_and_locks_member_editing(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.CORRECTION, subject="x")
        portal_svc.submit_request(req)
        req.refresh_from_db()
        self.assertEqual(req.status, PortalRequest.Status.SUBMITTED)
        self.assertFalse(req.member_may_edit)
        self.assertIsNotNone(req.submitted_at)

    def test_asking_for_more_information_reopens_it_to_the_member(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.CORRECTION, subject="x",
            submit=True)
        portal_svc.request_more_information(
            req, user=self.officer, message="Please attach the receipt.")
        req.refresh_from_db()
        self.assertEqual(req.status, PortalRequest.Status.INFO_NEEDED)
        self.assertTrue(req.member_may_edit)
        self.assertEqual(req.messages.count(), 1)

    def test_declining_without_a_reason_is_refused(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.CORRECTION, subject="x",
            submit=True)
        with self.assertRaises(ValidationError):
            portal_svc.decline_request(req, user=self.officer, reason="   ")

    def test_a_decided_request_cannot_be_withdrawn(self):
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.CORRECTION, subject="x",
            submit=True)
        portal_svc.decline_request(req, user=self.officer, reason="Already correct.")
        with self.assertRaises(ValidationError):
            portal_svc.withdraw_request(req)

    def test_a_death_report_must_say_who_died(self):
        req = PortalRequest(
            reference="REQ-TEST-0001", account=self.account,
            membership=self.membership, kind=PortalRequest.Kind.DEATH,
            subject="Bereavement", event_date=dt.date.today())
        with self.assertRaises(ValidationError):
            req.full_clean()

    def test_approving_a_correction_changes_no_money(self):
        """A correction request is an accepted point, not an automatic edit.
        The accounting change is made where accounting changes are made."""
        from .models import BenevolentContribution
        before = BenevolentContribution.objects.count()
        req = portal_svc.create_request(
            self.account, kind=PortalRequest.Kind.CORRECTION,
            subject="March contribution missing", submit=True)
        portal_svc.approve_request(req, user=self.officer,
                                   note="Checked — we will post the adjustment.")
        req.refresh_from_db()
        self.assertEqual(req.status, PortalRequest.Status.APPROVED)
        self.assertEqual(BenevolentContribution.objects.count(), before,
                         "Approving a correction must not itself move money.")


class PortalAccessLogTests(TestCase):
    def setUp(self):
        self.scheme = _scheme()
        self.ruth = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        _enrol(self.scheme, self.ruth)
        self.account = portal_svc.activate(portal_svc.invite(self.ruth))

    def test_reads_are_recorded(self):
        from .models import PortalAccessLog
        portal_svc.log_access(self.account, PortalAccessLog.Action.VIEW_CONTRIBUTIONS)
        self.assertEqual(
            PortalAccessLog.objects.filter(account=self.account).count(), 1)

    def test_a_broken_log_never_breaks_the_page(self):
        """An audit write must not be able to take down the thing it audits."""
        portal_svc.log_access(self.account, "NOT_A_REAL_ACTION" * 40)   # too long
        # no exception is the assertion


class PortalScopeDisciplineTests(TestCase):
    """A guard on the convention the whole portal rests on.

    Every portal queryset is supposed to come from ``services.portal.Scope``.
    The behavioural tests above prove the *current* views obey that; nothing
    stops a future one from reaching for a manager directly, and the failure
    mode is silent — a view that queries `BenevolentContribution.objects` works
    perfectly in development, where the developer is the only member.

    So this reads the view module and fails on a bare manager access. It is a
    blunt instrument, and deliberately so: the alternative is trusting review to
    catch, forever, the one mistake that leaks another family's record.
    """

    ALLOWED_BARE_MANAGERS = {
        # Creating a document row is a write against the member's own account,
        # not a read of anyone's data — there is nothing to scope.
        "PortalDocument.objects.create",
        # Event types are scheme configuration, not member data. Both uses are
        # already narrowed to schemes the member is enrolled in.
        "BenevolentEventType.objects.filter",
    }

    def test_portal_views_do_not_query_managers_directly(self):
        import pathlib
        import re

        source = (pathlib.Path(__file__).parent / "views_portal.py").read_text()
        offenders = []
        for line_no, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            for match in re.finditer(r"\b([A-Z]\w+)\.objects\.(\w+)", line):
                expression = f"{match.group(1)}.objects.{match.group(2)}"
                if expression not in self.ALLOWED_BARE_MANAGERS:
                    offenders.append(f"  line {line_no}: {expression}")

        self.assertFalse(
            offenders,
            "A portal view queries a model manager directly instead of going "
            "through services.portal.Scope. Object-level scoping is the only "
            "thing keeping one member out of another's record:\n"
            + "\n".join(offenders))
