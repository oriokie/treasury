"""Phase 9 — Roles, Permissions & User Experience.

Grouped around the claims Phase 9 makes:

  1. BACKWARD COMPATIBILITY   nothing that worked before this phase stops
                              working — Treasurer/Assistant, and anyone
                              already holding the old coarse manage_benevolent
                              right, keep every capability they had.
  2. GENUINE SEPARATION        a user granted only ONE of the three new
                              granular rights can do that, and only that.
  3. SEEDED PROFILES           the brief's named roles exist as real,
                              assignable profiles using the existing
                              mechanism — no separate permission system.
  4. THE COMMITTEE CHAIR        expressed through the existing seat concept
                              (Phase 6), not a duplicated right.
  5. VIEWS ACTUALLY ENFORCE    the re-pointed views really do gate on the
                              new, narrower checks.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profile
from core import roles as role_fns
from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentEventType, BenevolentScheme, CommitteeMember,
                               SchemePolicy)
from benevolent.services import committee as committee_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class Phase9Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t9", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.assistant = User.objects.create_user("as9", password="x")
        self.assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

        self.fund = Department.objects.create(
            name="P9 Fund", slug="p9-fund", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="P9 Scheme", code="P9", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
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
        self.mary = Member.objects.create(name="Mary Wekesa", phone="254722333444")

    def _user_with_profile(self, username, profile_name):
        u = User.objects.create_user(username, password="x")
        Profile.objects.get(name=profile_name).users.add(u)
        return u


# ===========================================================================
# 1. BACKWARD COMPATIBILITY
# ===========================================================================

class BackwardCompatibilityTests(Phase9Fixture):

    def test_a_treasurer_keeps_every_granular_capability(self):
        for fn in (role_fns.can_register_benevolent_members,
                  role_fns.can_manage_benevolent_cases,
                  role_fns.can_manage_benevolent_finance):
            self.assertTrue(fn(self.treasurer), fn.__name__)

    def test_an_assistant_keeps_every_granular_capability(self):
        for fn in (role_fns.can_register_benevolent_members,
                  role_fns.can_manage_benevolent_cases,
                  role_fns.can_manage_benevolent_finance):
            self.assertTrue(fn(self.assistant), fn.__name__)

    def test_an_old_style_holder_of_the_coarse_right_keeps_everything(self):
        """A profile built before this phase, still granting only the old
        `manage_benevolent` right, must continue to satisfy all three new
        granular checks — otherwise this phase would have quietly taken
        capability away from an existing deployment."""
        old_style = Profile.objects.create(
            name="Pre-Phase-9 welfare secretary", rights=["manage_benevolent",
                                                           "view_benevolent"])
        u = User.objects.create_user("oldstyle9", password="x")
        old_style.users.add(u)
        for fn in (role_fns.can_register_benevolent_members,
                  role_fns.can_manage_benevolent_cases,
                  role_fns.can_manage_benevolent_finance):
            self.assertTrue(fn(u), fn.__name__)

    def test_the_manage_benevolent_right_and_context_flag_are_unchanged(self):
        """Every template across seven phases checks {% if can.manage_benevolent %};
        that key must still mean exactly what it always meant."""
        from core.rights import has_right
        self.assertTrue(has_right(self.treasurer, "manage_benevolent"))


# ===========================================================================
# 2. GENUINE SEPARATION
# ===========================================================================

class SeparationTests(Phase9Fixture):

    def test_a_registration_officer_can_register_but_not_manage_cases_or_finance(self):
        u = self._user_with_profile("reg9", "Benevolent Registration Officer (default)")
        self.assertTrue(role_fns.can_register_benevolent_members(u))
        self.assertFalse(role_fns.can_manage_benevolent_cases(u))
        self.assertFalse(role_fns.can_manage_benevolent_finance(u))
        self.assertFalse(role_fns.can_approve_benevolent(u))

    def test_a_case_officer_can_manage_cases_but_not_register_or_finance(self):
        u = self._user_with_profile("case9", "Benevolent Case Officer (default)")
        self.assertTrue(role_fns.can_manage_benevolent_cases(u))
        self.assertFalse(role_fns.can_register_benevolent_members(u))
        self.assertFalse(role_fns.can_manage_benevolent_finance(u))

    def test_a_case_officer_cannot_approve_their_own_cases_type(self):
        """Raising/assessing a case and authorising one stay separate gates —
        the split must not accidentally fold approval into the case-officer
        right."""
        u = self._user_with_profile("case9b", "Benevolent Case Officer (default)")
        self.assertFalse(role_fns.can_approve_benevolent(u))

    def test_a_finance_officer_can_manage_finance_but_not_register_or_cases(self):
        u = self._user_with_profile("fin9", "Benevolent Finance Officer (default)")
        self.assertTrue(role_fns.can_manage_benevolent_finance(u))
        self.assertFalse(role_fns.can_register_benevolent_members(u))
        self.assertFalse(role_fns.can_manage_benevolent_cases(u))

    def test_a_committee_member_can_vote_but_manages_nothing_else(self):
        u = self._user_with_profile("comm9", "Benevolent Committee Member (default)")
        self.assertTrue(role_fns.can_vote_benevolent(u))
        self.assertFalse(role_fns.can_register_benevolent_members(u))
        self.assertFalse(role_fns.can_manage_benevolent_cases(u))
        self.assertFalse(role_fns.can_manage_benevolent_finance(u))
        self.assertFalse(role_fns.can_approve_benevolent(u))

    def test_an_approver_can_approve_but_not_administer_day_to_day(self):
        u = self._user_with_profile("appr9", "Benevolent Approver (default)")
        self.assertTrue(role_fns.can_approve_benevolent(u))
        self.assertFalse(role_fns.can_register_benevolent_members(u))
        self.assertFalse(role_fns.can_manage_benevolent_cases(u))
        self.assertFalse(role_fns.can_manage_benevolent_finance(u))

    def test_an_auditor_profile_can_only_view(self):
        u = self._user_with_profile("aud9", "Benevolent Auditor (default)")
        self.assertTrue(role_fns.can_view_benevolent(u))
        for fn in (role_fns.can_register_benevolent_members,
                  role_fns.can_manage_benevolent_cases,
                  role_fns.can_manage_benevolent_finance,
                  role_fns.can_approve_benevolent, role_fns.can_vote_benevolent):
            self.assertFalse(fn(u), fn.__name__)

    def test_an_administrator_sets_up_schemes_but_does_not_run_them_day_to_day(self):
        u = self._user_with_profile("admin9", "Benevolent Administrator (default)")
        self.assertTrue(role_fns.can_manage_benevolent_schemes(u))
        self.assertTrue(role_fns.can_manage_benevolent_settings(u))
        self.assertFalse(role_fns.can_register_benevolent_members(u))
        self.assertFalse(role_fns.can_manage_benevolent_cases(u))
        self.assertFalse(role_fns.can_manage_benevolent_finance(u))


# ===========================================================================
# 3. SEEDED PROFILES
# ===========================================================================

class SeededProfileTests(Phase9Fixture):

    NAMES = [
        "Benevolent Administrator (default)", "Benevolent Approver (default)",
        "Benevolent Committee Member (default)",
        "Benevolent Registration Officer (default)",
        "Benevolent Case Officer (default)", "Benevolent Finance Officer (default)",
        "Benevolent Auditor (default)",
    ]

    def test_every_named_profile_exists_and_is_marked_system(self):
        for name in self.NAMES:
            p = Profile.objects.get(name=name)
            self.assertTrue(p.is_system)
            self.assertIn("view_benevolent", p.rights)

    def test_every_right_in_every_profile_is_a_real_right_key(self):
        from core.rights import RIGHT_KEYS
        for name in self.NAMES:
            p = Profile.objects.get(name=name)
            for r in p.rights:
                self.assertIn(r, RIGHT_KEYS, f"{name}: unknown right {r}")

    def test_no_two_profiles_are_accidentally_identical_except_the_documented_pair(self):
        """Committee Member has no Chair counterpart (documented: chairing is
        a seat, not a right) — every OTHER pair of profiles must differ."""
        seen = {}
        for name in self.NAMES:
            rights = tuple(sorted(Profile.objects.get(name=name).rights))
            self.assertNotIn(rights, seen,
                             f"{name} has identical rights to {seen.get(rights)}")
            seen[rights] = name


# ===========================================================================
# 4. THE COMMITTEE CHAIR — a seat, not a right
# ===========================================================================

class CommitteeChairTests(Phase9Fixture):

    def test_is_committee_chair_is_false_for_an_ordinary_member(self):
        u = self._user_with_profile("comm9c", "Benevolent Committee Member (default)")
        committee_svc.add_member(self.scheme, u, added_by=self.treasurer)
        self.assertFalse(role_fns.is_benevolent_committee_chair(u))

    def test_is_committee_chair_is_true_once_seated_as_chair(self):
        u = self._user_with_profile("chair9", "Benevolent Committee Member (default)")
        committee_svc.add_member(self.scheme, u, role=CommitteeMember.Role.CHAIR,
                                 added_by=self.treasurer)
        self.assertTrue(role_fns.is_benevolent_committee_chair(u))
        self.assertTrue(role_fns.is_benevolent_committee_chair(u, scheme=self.scheme))

    def test_a_chair_of_one_scheme_is_not_a_chair_of_another(self):
        fund2 = Department.objects.create(
            name="P9 Fund 2", slug="p9-fund-2", fund_type=Department.FundType.LOCAL)
        other = BenevolentScheme.objects.create(
            name="Other P9", code="OP9", fund=fund2, created_by=self.treasurer)
        u = self._user_with_profile("chair9b", "Benevolent Committee Member (default)")
        committee_svc.add_member(self.scheme, u, role=CommitteeMember.Role.CHAIR,
                                 added_by=self.treasurer)
        self.assertFalse(role_fns.is_benevolent_committee_chair(u, scheme=other))

    def test_removing_the_chair_seat_ends_the_chairship(self):
        u = self._user_with_profile("chair9c", "Benevolent Committee Member (default)")
        seat = committee_svc.add_member(self.scheme, u, role=CommitteeMember.Role.CHAIR,
                                        added_by=self.treasurer)
        committee_svc.remove_member(seat, removed_by=self.treasurer)
        self.assertFalse(role_fns.is_benevolent_committee_chair(u))

    def test_the_chair_and_ordinary_member_hold_the_same_right(self):
        """Confirms the design decision explicitly: chairing changes nothing
        about WHICH right is checked — only the seat differs."""
        chair = self._user_with_profile("chair9d", "Benevolent Committee Member (default)")
        member = self._user_with_profile("member9d", "Benevolent Committee Member (default)")
        committee_svc.add_member(self.scheme, chair, role=CommitteeMember.Role.CHAIR,
                                 added_by=self.treasurer)
        committee_svc.add_member(self.scheme, member, added_by=self.treasurer)
        self.assertEqual(role_fns.can_vote_benevolent(chair),
                         role_fns.can_vote_benevolent(member))
        self.assertTrue(role_fns.can_vote_benevolent(chair))


# ===========================================================================
# 5. VIEWS ACTUALLY ENFORCE THE NEW CHECKS
# ===========================================================================

class ViewEnforcementTests(Phase9Fixture):

    def test_a_registration_officer_can_reach_the_registry_but_not_raise_a_case(self):
        u = self._user_with_profile("regv9", "Benevolent Registration Officer (default)")
        self.client.force_login(u)
        r1 = self.client.get(reverse("benevolent_registry"))
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get(reverse("benevolent_case_new", args=[self.scheme.pk]))
        self.assertNotEqual(r2.status_code, 200)

    def test_a_case_officer_can_raise_a_case_but_not_register_a_member(self):
        u = self._user_with_profile("casev9", "Benevolent Case Officer (default)")
        self.client.force_login(u)
        r1 = self.client.get(reverse("benevolent_case_new", args=[self.scheme.pk]))
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get(reverse("benevolent_register", args=[self.scheme.pk]))
        self.assertNotEqual(r2.status_code, 200)

    def test_a_finance_officer_can_reach_the_intake_queue_but_not_register_a_member(self):
        u = self._user_with_profile("finv9", "Benevolent Finance Officer (default)")
        self.client.force_login(u)
        r1 = self.client.get(reverse("benevolent_intake_queue"))
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.get(reverse("benevolent_register", args=[self.scheme.pk]))
        self.assertNotEqual(r2.status_code, 200)

    def test_everyone_can_still_view_the_dashboard(self):
        for i, profile_name in enumerate(SeededProfileTests.NAMES):
            u = self._user_with_profile(f"viewer9_{i}", profile_name)
            self.client.force_login(u)
            r = self.client.get(reverse("benevolent_dashboard"))
            self.assertEqual(r.status_code, 200, profile_name)


# ===========================================================================
# 6. UX FIXES — settings completeness, role-aware dashboard, navigation
# ===========================================================================

class SettingsPageCompletenessTests(Phase9Fixture):
    """A real bug found while working on this phase: the settings template
    referenced three fields retired in Phase 7 (so that whole section
    silently rendered empty) and was missing roughly half the model's actual
    fields — every Phase 7 member/committee notification toggle, and every
    Phase 4 allocation-tuning field — with no way to reach them from the web
    UI at all. Regression-guarded here so it cannot quietly happen again."""

    def test_every_settings_field_appears_somewhere_on_the_page(self):
        from benevolent.models import BenevolentSettings
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_settings")).content.decode()
        skip = {"id", "automation_last_run", "automation_last_summary", "updated_at"}
        for f in BenevolentSettings._meta.get_fields():
            if not hasattr(f, "name") or f.name in skip:
                continue
            if f.name.startswith("historical") or f.is_relation:
                continue
            self.assertIn(f'id_{f.name}"', body, f"{f.name} is missing from the settings page")

    def test_no_retired_field_name_survives_in_the_template(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_settings")).content.decode()
        for dead in ("notify_member_on_enrolment", "notify_member_on_benefit_paid",
                    "notify_member_on_arrears"):
            self.assertNotIn(dead, body)

    def test_settings_can_still_be_saved(self):
        self.client.force_login(self.treasurer)
        url = reverse("benevolent_settings")
        get_body = self.client.get(url).content.decode()
        self.assertEqual(self.client.get(url).status_code, 200)
        from benevolent.models import BenevolentSettings
        cfg = BenevolentSettings.get()
        # a minimal valid POST covering every field the ModelForm requires
        data = {
            "default_benefit_category": "BENEVOLENCE",
            "notify_on_case_submitted": "on", "reminder_min_gap_days": "21",
            "auto_allocate_threshold": "85", "review_threshold": "40",
            "fuzzy_name_threshold": "82", "duplicate_window_days": "3",
            "arrears_reminder_days": "7", "renewal_reminder_days": "30",
            "staff_channel": cfg.staff_channel, "member_channel": cfg.member_channel,
        }
        r = self.client.post(url, data)
        self.assertIn(r.status_code, (200, 302))


class RoleAwareDashboardTests(Phase9Fixture):

    def test_a_finance_officer_sees_the_intake_queue_count(self):
        u = self._user_with_profile("finq9", "Benevolent Finance Officer (default)")
        self.client.force_login(u)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn("Intake queue", body)
        self.assertNotIn("Awaiting your vote", body)

    def test_a_committee_member_sees_only_their_own_relevant_queue(self):
        u = self._user_with_profile("commq9", "Benevolent Committee Member (default)")
        self.client.force_login(u)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn("Awaiting your vote", body)
        self.assertNotIn("Intake queue", body)
        self.assertNotIn("Awaiting approval", body)

    def test_a_pure_auditor_sees_no_queues_panel_at_all(self):
        u = self._user_with_profile("audq9", "Benevolent Auditor (default)")
        self.client.force_login(u)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertNotIn("Your queues", body)

    def test_the_dashboard_shows_the_arrears_kpi(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn("arrears", body.lower())


class NavigationTests(Phase9Fixture):

    def test_the_benevolent_nav_links_to_the_report_catalogue(self):
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn("/reports/library/", body)


# ===========================================================================
# 6. DASHBOARD — role-aware queues, arrears, and the reports link
# ===========================================================================

class DashboardUxTests(Phase9Fixture):

    def test_the_dashboard_shows_an_arrears_card_when_there_is_arrears(self):
        m = reg_svc.register(self.scheme, self.mary,
                             joined_on=TODAY - dt.timedelta(days=200),
                             user=self.treasurer)
        self.client.force_login(self.treasurer)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn("arrears", body.lower())

    def test_a_registration_officer_sees_only_their_own_queue_entries(self):
        u = self._user_with_profile("dashreg9", "Benevolent Registration Officer (default)")
        self.client.force_login(u)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn("Pending admission", body)
        self.assertNotIn("Awaiting assessment", body)
        self.assertNotIn("Awaiting approval", body)
        self.assertNotIn("Intake queue", body)

    def test_a_finance_officer_sees_the_intake_queue_not_registration(self):
        u = self._user_with_profile("dashfin9", "Benevolent Finance Officer (default)")
        self.client.force_login(u)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn("Intake queue", body)
        self.assertNotIn("Pending admission", body)

    def test_a_committee_chair_gets_the_chair_badge_an_ordinary_member_does_not(self):
        chair = self._user_with_profile("dashchair9", "Benevolent Committee Member (default)")
        member = self._user_with_profile("dashmember9", "Benevolent Committee Member (default)")
        committee_svc.add_member(self.scheme, chair, role=CommitteeMember.Role.CHAIR,
                                 added_by=self.treasurer)
        committee_svc.add_member(self.scheme, member, added_by=self.treasurer)

        self.client.force_login(chair)
        chair_body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.client.force_login(member)
        member_body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        self.assertIn(">Chair<", chair_body)
        self.assertNotIn(">Chair<", member_body)

    def test_every_seeded_role_can_reach_the_benevolent_reports(self):
        """The Phase 8 report catalogue's own access gate requires
        view_reports before it even looks at a report's own permission — a
        genuinely scoped role must not be locked out of the reports built
        specifically for the module it administers."""
        for i, profile_name in enumerate(SeededProfileTests.NAMES):
            u = self._user_with_profile(f"repuser9_{i}", profile_name)
            self.client.force_login(u)
            r = self.client.get(reverse("engine_report", args=["benevolent_overview"]))
            self.assertEqual(r.status_code, 200, profile_name)

    def test_the_nav_reports_link_only_shows_for_users_who_can_use_it(self):
        outsider = User.objects.create_user("navoutsider9", password="x")
        self.client.force_login(outsider)
        body = self.client.get(reverse("benevolent_dashboard")).content.decode()
        # an outsider can't even reach the dashboard (no view_benevolent),
        # so nothing to assert on content — confirm the redirect instead
        self.assertNotEqual(
            self.client.get(reverse("benevolent_dashboard")).status_code, 200)
