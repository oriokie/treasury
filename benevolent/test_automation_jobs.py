"""Item 7 — automation jobs and review tasks.

The design decision behind these tests: automation NEVER changes a membership's
status. Where it detects that a status change is due (suspend, close, drop a
dependant's cover), it raises a review task and leaves the status untouched, so
these tests assert both that the task was raised AND that the status did not move.
The safe housekeeping jobs (archive, eligibility/duplicate flags) are tested for
their effect directly.
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
                               BenevolentTask, CaseEvent, SchemeDependant,
                               SchemeMembership, SchemePolicy)
from benevolent.services import automation as auto_svc
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class AutomationFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_auto", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Auto Fund", slug="auto-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Auto Scheme", code="AUT", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER", triggers_on_death=True)
        cfg = BenevolentSettings.get()
        cfg.automation_enabled = True
        cfg.auto_flag_inactive = True
        cfg.save()

    def _publish(self, **overrides):
        kwargs = dict(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=900),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        kwargs.update(overrides)
        policy = SchemePolicy.objects.create(**kwargs)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        if self.scheme.status == BenevolentScheme.Status.DRAFT:
            scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        return policy

    def _enrol(self, name, days_ago, phone=""):
        m = Member.objects.create(name=name, phone=phone)
        return reg_svc.register(
            self.scheme, m, joined_on=TODAY - dt.timedelta(days=days_ago),
            user=self.treasurer)


# ---------------------------------------------------------------------------
# Suspend overdue — PROPOSE, never act
# ---------------------------------------------------------------------------

class SuspendProposalTests(AutomationFixture):
    def test_overdue_member_raises_task_but_status_unchanged(self):
        self._publish(inactivity_months=3,
                      inactivity_action=SchemePolicy.InactivityAction.SUSPEND)
        mem = self._enrol("Overdue Member", 200)   # no contributions, 6+ mo idle
        auto_svc.propose_suspensions(self.scheme)
        # a task was raised
        task = BenevolentTask.objects.filter(
            kind=BenevolentTask.Kind.SUSPEND_OVERDUE, membership=mem).first()
        self.assertIsNotNone(task)
        self.assertEqual(task.recommended_action,
                         SchemePolicy.InactivityAction.SUSPEND)
        # BUT the member is still ACTIVE — automation did not act
        mem.refresh_from_db()
        self.assertEqual(mem.status, SchemeMembership.Status.ACTIVE)

    def test_policy_flag_only_raises_nothing(self):
        # a policy whose inactivity action is FLAG (not suspend/lapse) raises no
        # suspension task — there is no status change to propose
        self._publish(inactivity_months=3,
                      inactivity_action=SchemePolicy.InactivityAction.FLAG)
        self._enrol("Flagged Member", 200)
        raised = auto_svc.propose_suspensions(self.scheme)
        self.assertEqual(raised, 0)

    def test_current_member_not_flagged(self):
        self._publish(inactivity_months=3,
                      inactivity_action=SchemePolicy.InactivityAction.SUSPEND)
        mem = self._enrol("Current Member", 120)
        for i in range(4):
            contrib_svc.record_contribution(
                self.scheme, date=TODAY - dt.timedelta(days=90 - i * 30),
                amount=Decimal("100"), user=self.treasurer, membership=mem)
        raised = auto_svc.propose_suspensions(self.scheme)
        self.assertEqual(raised, 0)

    def test_idempotent(self):
        self._publish(inactivity_months=3,
                      inactivity_action=SchemePolicy.InactivityAction.SUSPEND)
        self._enrol("Repeat Overdue", 200)
        auto_svc.propose_suspensions(self.scheme)
        auto_svc.propose_suspensions(self.scheme)
        auto_svc.propose_suspensions(self.scheme)
        self.assertEqual(BenevolentTask.objects.filter(
            kind=BenevolentTask.Kind.SUSPEND_OVERDUE).count(), 1)


# ---------------------------------------------------------------------------
# Close inactive — PROPOSE
# ---------------------------------------------------------------------------

class CloseProposalTests(AutomationFixture):
    def test_long_suspended_member_proposed_for_closure(self):
        self._publish()
        mem = self._enrol("Suspended Long", 900)
        reg_svc.suspend(mem, user=self.treasurer, reason="stopped paying")
        raised = auto_svc.propose_closures(self.scheme, idle_months=24)
        self.assertEqual(raised, 1)
        task = BenevolentTask.objects.filter(
            kind=BenevolentTask.Kind.CLOSE_INACTIVE, membership=mem).first()
        self.assertIsNotNone(task)
        # still suspended, not closed
        mem.refresh_from_db()
        self.assertEqual(mem.status, SchemeMembership.Status.SUSPENDED)


# ---------------------------------------------------------------------------
# Waiting period served — NOTIFY
# ---------------------------------------------------------------------------

class WaitingPeriodTests(AutomationFixture):
    def test_member_who_just_became_eligible_flagged(self):
        self._publish(waiting_period_days=30)
        self._enrol("Newly Eligible", 31)   # crossed 30-day line yesterday
        raised = auto_svc.flag_waiting_period_served(self.scheme)
        self.assertEqual(raised, 1)

    def test_long_eligible_member_not_reflagged(self):
        self._publish(waiting_period_days=30)
        self._enrol("Long Eligible", 200)   # well past the window
        raised = auto_svc.flag_waiting_period_served(self.scheme)
        self.assertEqual(raised, 0)


# ---------------------------------------------------------------------------
# Age dependants — PROPOSE
# ---------------------------------------------------------------------------

class DependantAgingTests(AutomationFixture):
    def test_aged_out_child_raises_task_but_cover_kept(self):
        self._publish(dependant_age_limit=18)
        mem = self._enrol("Parent", 200)
        dep = SchemeDependant.objects.create(
            membership=mem, name="Grown Kid",
            relationship=SchemeDependant.Relationship.CHILD,
            date_of_birth=dt.date(TODAY.year - 20, 1, 1))
        auto_svc.flag_aged_out_dependants(self.scheme)
        task = BenevolentTask.objects.filter(
            kind=BenevolentTask.Kind.DEPENDANT_AGED_OUT, dependant=dep).first()
        self.assertIsNotNone(task)
        # dependant is still active — cover not dropped automatically
        dep.refresh_from_db()
        self.assertTrue(dep.active)

    def test_child_within_limit_not_flagged(self):
        self._publish(dependant_age_limit=18)
        mem = self._enrol("Parent2", 200)
        SchemeDependant.objects.create(
            membership=mem, name="Young Kid",
            relationship=SchemeDependant.Relationship.CHILD,
            date_of_birth=dt.date(TODAY.year - 10, 1, 1))
        raised = auto_svc.flag_aged_out_dependants(self.scheme)
        self.assertEqual(raised, 0)


# ---------------------------------------------------------------------------
# Detect duplicates — PROPOSE
# ---------------------------------------------------------------------------

class DuplicateDetectionTests(AutomationFixture):
    def test_same_name_and_phone_flagged(self):
        self._publish()
        self._enrol("John Mwangi", 100, phone="254700111222")
        self._enrol("John Mwangi", 90, phone="254700111222")
        raised = auto_svc.flag_duplicate_memberships(self.scheme)
        self.assertEqual(raised, 1)

    def test_shared_family_phone_different_names_not_flagged(self):
        self._publish()
        self._enrol("Alice Wanjiru", 100, phone="254700111222")
        self._enrol("Bob Wanjiru", 90, phone="254700111222")
        raised = auto_svc.flag_duplicate_memberships(self.scheme)
        self.assertEqual(raised, 0)


# ---------------------------------------------------------------------------
# Archive completed cases — HOUSEKEEPING
# ---------------------------------------------------------------------------

class ArchiveTests(AutomationFixture):
    def test_old_closed_case_archived(self):
        self._publish()
        mem = self._enrol("Archive Member", 400)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=300), membership=mem,
            user=self.treasurer)
        # force it closed a long time ago
        case.status = BenevolentCase.Status.CLOSED
        from django.utils import timezone
        case.closed_at = timezone.now() - dt.timedelta(days=250)
        case.save(update_fields=["status", "closed_at"])
        archived = auto_svc.archive_completed_cases(self.scheme, closed_months=6)
        self.assertEqual(archived, 1)
        self.assertTrue(case.events.filter(kind=CaseEvent.Kind.ARCHIVED).exists())

    def test_recent_closed_case_not_archived(self):
        self._publish()
        mem = self._enrol("Recent Member", 100)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=mem, user=self.treasurer)
        from django.utils import timezone
        case.status = BenevolentCase.Status.CLOSED
        case.closed_at = timezone.now() - dt.timedelta(days=10)
        case.save(update_fields=["status", "closed_at"])
        archived = auto_svc.archive_completed_cases(self.scheme, closed_months=6)
        self.assertEqual(archived, 0)

    def test_archive_idempotent(self):
        self._publish()
        mem = self._enrol("Idem Member", 400)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=300), membership=mem,
            user=self.treasurer)
        from django.utils import timezone
        case.status = BenevolentCase.Status.CLOSED
        case.closed_at = timezone.now() - dt.timedelta(days=250)
        case.save(update_fields=["status", "closed_at"])
        auto_svc.archive_completed_cases(self.scheme, closed_months=6)
        auto_svc.archive_completed_cases(self.scheme, closed_months=6)
        self.assertEqual(
            case.events.filter(kind=CaseEvent.Kind.ARCHIVED).count(), 1)


# ---------------------------------------------------------------------------
# Task mechanism + integration
# ---------------------------------------------------------------------------

class TaskMechanismTests(AutomationFixture):
    def test_resolve_task_records_but_changes_no_status(self):
        self._publish(inactivity_months=3,
                      inactivity_action=SchemePolicy.InactivityAction.SUSPEND)
        mem = self._enrol("Resolve Member", 200)
        auto_svc.propose_suspensions(self.scheme)
        task = BenevolentTask.objects.get(kind=BenevolentTask.Kind.SUSPEND_OVERDUE)
        auto_svc.resolve_task(task, user=self.treasurer, action="dismiss",
                              note="member paid in cash")
        task.refresh_from_db()
        self.assertEqual(task.status, BenevolentTask.Status.DISMISSED)
        mem.refresh_from_db()
        self.assertEqual(mem.status, SchemeMembership.Status.ACTIVE)

    def test_run_jobs_returns_tally(self):
        self._publish(inactivity_months=3,
                      inactivity_action=SchemePolicy.InactivityAction.SUSPEND,
                      dependant_age_limit=18, waiting_period_days=30)
        self._enrol("Tally Overdue", 200)
        tally = auto_svc.run_jobs(scheme=self.scheme)
        self.assertIn("suspensions_proposed", tally)
        self.assertGreaterEqual(tally["suspensions_proposed"], 1)

    def test_run_automation_includes_jobs(self):
        self._publish(inactivity_months=3,
                      inactivity_action=SchemePolicy.InactivityAction.SUSPEND)
        self._enrol("Full Run Overdue", 200)
        result = scheme_svc.run_automation(scheme=self.scheme, force=True)
        self.assertIn("jobs", result)
        self.assertIn("review task", result["summary"])

    def test_task_list_page_renders(self):
        self._publish()
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_task_list"))
        self.assertEqual(r.status_code, 200)

    def test_resolve_view(self):
        self._publish(inactivity_months=3,
                      inactivity_action=SchemePolicy.InactivityAction.SUSPEND)
        self._enrol("View Resolve", 200)
        auto_svc.propose_suspensions(self.scheme)
        task = BenevolentTask.objects.get(kind=BenevolentTask.Kind.SUSPEND_OVERDUE)
        self.client.force_login(self.treasurer)
        self.client.post(reverse("benevolent_task_resolve", args=[task.pk]),
                         {"action": "done", "note": "suspended by hand"}, follow=True)
        task.refresh_from_db()
        self.assertEqual(task.status, BenevolentTask.Status.DONE)

    def test_settings_not_rule_fields(self):
        self.assertNotIn("auto_archive_cases", SchemePolicy.RULE_FIELDS)
