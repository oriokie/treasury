"""Phase 7 — Financial Integration & Communications.

Grouped around the claims Phase 7 makes:

  1. TEMPLATES          editable, placeholder-driven, reused across SMS/email.
  2. DELIVERY           send() reuses the existing SMS/email engines, logs
                        history, never raises.
  3. EVENT WIRING        the events named in the brief actually fire, to the
                        actual member/committee — not staff standing in for
                        them (the bug this phase found and fixed).
  4. RETRIES             bounded, only touches FAILED rows.
  5. DUE REMINDERS       closes a gap that survived three phases untouched.
  6. FINANCIAL INTEGRATION — confirmed, not rebuilt: a full case lifecycle
     still balances the general ledger exactly as before.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentNotification,
                               BenevolentScheme, BenevolentSettings, CommitteeMember,
                               NotificationEvent, NotificationTemplate, SchemeMembership,
                               SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import committee as committee_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import notify as notify_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class Phase7Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t7", password="x", email="t7@example.com")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c7", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

        self.fund = Department.objects.create(
            name="P7 Fund", slug="p7-fund", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="P7 Scheme", code="P7", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            renewal_required=True, renewal_fee=Decimal("50"),
            renewal_period=SchemePolicy.RenewalPeriod.ANNUAL,
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(self.policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

        notify_svc.install_default_templates()

        # SMS/email stay unconfigured by default here, matching the existing
        # test convention elsewhere in the app (core/tests.py) — no test in
        # this file should depend on a real network call succeeding. Tests
        # that need to prove the SENT pathway mock core.services.sms.send_sms
        # / core.services.email.send_email explicitly.
        from core.models import SiteConfig
        site = SiteConfig.get()
        site.sms_enabled = False
        site.save()

        self.mary = Member.objects.create(name="Mary Kioko", phone="254711222333")

    def _membership(self, days_ago=90):
        return reg_svc.register(self.scheme, self.mary,
                                joined_on=TODAY - dt.timedelta(days=days_ago),
                                user=self.treasurer)

    def _assessed_case(self, membership):
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=membership, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=2), reported_date=TODAY,
            raised_by=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        case_svc.assess_case(case, user=self.treasurer)
        return case


# ===========================================================================
# 1. TEMPLATES
# ===========================================================================

class TemplateTests(Phase7Fixture):

    def test_installing_defaults_creates_every_event_times_channel(self):
        count = NotificationEvent.choices
        self.assertEqual(
            NotificationTemplate.objects.count(), len(count) * 2)   # SMS + EMAIL each

    def test_installing_defaults_twice_does_not_duplicate(self):
        before = NotificationTemplate.objects.count()
        notify_svc.install_default_templates()
        self.assertEqual(NotificationTemplate.objects.count(), before)

    def test_installing_defaults_never_overwrites_an_edit(self):
        tpl = NotificationTemplate.objects.filter(
            event=NotificationEvent.REGISTRATION_CONFIRMED, channel="SMS").first()
        tpl.body = "A treasurer's own wording."
        tpl.save()
        notify_svc.install_default_templates()
        tpl.refresh_from_db()
        self.assertEqual(tpl.body, "A treasurer's own wording.")

    def test_force_does_overwrite(self):
        tpl = NotificationTemplate.objects.filter(
            event=NotificationEvent.REGISTRATION_CONFIRMED, channel="SMS").first()
        tpl.body = "Something else entirely."
        tpl.save()
        notify_svc.install_default_templates(force=True)
        tpl.refresh_from_db()
        self.assertNotEqual(tpl.body, "Something else entirely.")

    def test_placeholders_are_substituted(self):
        tpl = NotificationTemplate(event="X", channel="SMS",
                                   body="Hello {member_name}, you owe {amount}.")
        subject, body = tpl.render({"member_name": "Mary", "amount": "500"})
        self.assertEqual(body, "Hello Mary, you owe 500.")

    def test_an_unmatched_placeholder_is_left_literal_not_raised(self):
        tpl = NotificationTemplate(event="X", channel="SMS",
                                   body="Hello {member_name}, code {mystery}.")
        subject, body = tpl.render({"member_name": "Mary"})
        self.assertIn("{mystery}", body)
        self.assertIn("Mary", body)

    def test_sms_templates_have_no_subject_field_in_the_form(self):
        from benevolent.forms import NotificationTemplateForm
        tpl = NotificationTemplate.objects.filter(channel="SMS").first()
        form = NotificationTemplateForm(instance=tpl)
        self.assertNotIn("subject", form.fields)

    def test_email_templates_do_have_a_subject_field(self):
        from benevolent.forms import NotificationTemplateForm
        tpl = NotificationTemplate.objects.filter(channel="EMAIL").first()
        form = NotificationTemplateForm(instance=tpl)
        self.assertIn("subject", form.fields)


# ===========================================================================
# 2. DELIVERY & LOGGING
# ===========================================================================

class DeliveryTests(Phase7Fixture):

    def test_sending_with_no_recipient_creates_no_row(self):
        m = self._membership()
        m.member.phone = ""
        m.member.save()
        m.email = ""
        m.save()
        before = BenevolentNotification.objects.count()
        notify_svc.send(NotificationEvent.REGISTRATION_CONFIRMED, membership=m)
        self.assertEqual(BenevolentNotification.objects.count(), before)

    def test_sms_disabled_is_recorded_as_skipped_not_silently_dropped(self):
        m = self._membership()
        notify_svc.send(NotificationEvent.RENEWAL_CONFIRMED, membership=m)
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.RENEWAL_CONFIRMED, channel="SMS").first()
        self.assertIsNotNone(n)
        self.assertEqual(n.status, BenevolentNotification.Status.SKIPPED)

    def test_a_successful_sms_is_recorded_as_sent_and_linked_to_its_sms_log(self):
        from unittest import mock
        from core.models import SmsLog
        fake_log = SmsLog.objects.create(to="254711222333", message="x",
                                         status=SmsLog.Status.SENT, response="OK")
        m = self._membership()
        with mock.patch("core.services.sms.send_sms", return_value=fake_log):
            notify_svc.send(NotificationEvent.RENEWAL_CONFIRMED, membership=m)
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.RENEWAL_CONFIRMED, channel="SMS").first()
        self.assertEqual(n.status, BenevolentNotification.Status.SENT)
        self.assertEqual(n.sms_log, fake_log)
        self.assertIsNotNone(n.sent_at)

    def test_a_failed_sms_is_recorded_with_the_error(self):
        from unittest import mock
        from core.models import SmsLog
        fake_log = SmsLog.objects.create(to="254711222333", message="x",
                                         status=SmsLog.Status.FAILED,
                                         response="Network unreachable")
        m = self._membership()
        with mock.patch("core.services.sms.send_sms", return_value=fake_log):
            notify_svc.send(NotificationEvent.RENEWAL_CONFIRMED, membership=m)
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.RENEWAL_CONFIRMED, channel="SMS").first()
        self.assertEqual(n.status, BenevolentNotification.Status.FAILED)
        self.assertIn("Network unreachable", n.last_error)

    def test_email_not_configured_is_skipped_not_dropped(self):
        m = self._membership()
        m.email = "mary@example.com"
        m.save()
        notify_svc.send(NotificationEvent.RENEWAL_CONFIRMED, membership=m)
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.RENEWAL_CONFIRMED, channel="EMAIL").first()
        self.assertEqual(n.status, BenevolentNotification.Status.SKIPPED)

    def test_a_member_with_no_email_gets_no_email_row_but_can_still_get_sms(self):
        m = self._membership()
        self.assertEqual(m.email, "")
        results = notify_svc.send(NotificationEvent.RENEWAL_CONFIRMED, membership=m)
        channels = {r.channel for r in results}
        self.assertIn("SMS", channels)
        self.assertNotIn("EMAIL", channels)

    def test_an_inactive_template_sends_nothing_on_that_channel(self):
        tpl = NotificationTemplate.objects.get(
            event=NotificationEvent.RENEWAL_CONFIRMED, channel="SMS")
        tpl.active = False
        tpl.save()
        m = self._membership()
        results = notify_svc.send(NotificationEvent.RENEWAL_CONFIRMED, membership=m)
        self.assertFalse(any(r.channel == "SMS" for r in results))

    def test_send_never_raises_even_if_something_inside_explodes(self):
        from unittest import mock
        m = self._membership()
        with mock.patch("core.services.sms.send_sms", side_effect=RuntimeError("boom")):
            try:
                notify_svc.send(NotificationEvent.RENEWAL_CONFIRMED, membership=m)
            except Exception as e:  # noqa: BLE001
                self.fail(f"send() must never raise, got {e!r}")


# ===========================================================================
# 3. EVENT WIRING — the events actually fire, to the actual recipient
# ===========================================================================

class EventWiringTests(Phase7Fixture):

    def test_registering_with_auto_approval_notifies_the_member_immediately(self):
        before = BenevolentNotification.objects.count()
        m = self._membership()
        self.assertGreater(BenevolentNotification.objects.count(), before)
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.REGISTRATION_CONFIRMED, membership=m).first()
        self.assertIsNotNone(n)
        self.assertIn("MARY KIOKO", n.body)   # Member.save() stores names uppercase

    def test_registering_that_needs_admission_does_not_notify_until_admitted(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=400),
            user=self.treasurer)
        v2.registration_required = True
        v2.registration_approval = SchemePolicy.RegistrationApproval.TREASURER
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        m = reg_svc.register(self.scheme, self.mary, joined_on=TODAY,
                             user=self.treasurer)
        self.assertEqual(m.status, SchemeMembership.Status.PENDING)
        self.assertFalse(BenevolentNotification.objects.filter(
            event=NotificationEvent.REGISTRATION_CONFIRMED, membership=m).exists())

        reg_svc.admit(m, user=self.treasurer)
        self.assertTrue(BenevolentNotification.objects.filter(
            event=NotificationEvent.REGISTRATION_CONFIRMED, membership=m).exists())

    def test_this_fixes_a_real_bug_registry_notify_used_to_silently_message_staff(self):
        """The old registry._notify claimed ('Tell the member...') to notify
        the member but actually always messaged treasurers, gated by a field
        that was never wired to anything and therefore never true. Confirm
        the fix: a registration now produces a BenevolentNotification aimed
        at the MEMBER's own phone number, not a staff Notification."""
        from core.models import Notification
        staff_before = Notification.objects.filter(kind="GENERAL").count()
        m = self._membership()
        n = BenevolentNotification.objects.get(
            event=NotificationEvent.REGISTRATION_CONFIRMED, membership=m, channel="SMS")
        self.assertEqual(n.recipient, "254711222333")   # the MEMBER's own phone

    def test_paying_a_renewal_fee_notifies_the_member(self):
        m = self._membership()
        contrib_svc.record_fee(m, kind="RENEWAL", user=self.treasurer)
        self.assertTrue(BenevolentNotification.objects.filter(
            event=NotificationEvent.RENEWAL_CONFIRMED, membership=m).exists())

    def test_submitting_a_case_notifies_the_member_it_was_received(self):
        m = self._membership()
        case = BenevolentCase.objects.create(
            scheme=self.scheme, membership=m, event_type=self.bereavement,
            event_date=TODAY, reported_date=TODAY, raised_by=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.CASE_RECEIVED, case=case).first()
        self.assertIsNotNone(n)
        self.assertIn(case.number, n.body)

    def test_approving_a_case_notifies_the_member_it_was_decided(self):
        m = self._membership()
        case = self._assessed_case(m)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.treasurer,
                              allow_self_approval=True)
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.CASE_DECIDED, case=case).first()
        self.assertIsNotNone(n)
        self.assertIn("approved", n.body.lower())

    def test_rejecting_a_case_notifies_the_member_too(self):
        m = self._membership()
        case = self._assessed_case(m)
        case_svc.reject_case(case, reason="Not a covered event.", user=self.treasurer)
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.CASE_DECIDED, case=case).first()
        self.assertIsNotNone(n)

    def test_a_payout_clearing_notifies_the_member(self):
        m = self._membership()
        case = self._assessed_case(m)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.treasurer,
                              allow_self_approval=True)
        payout = case_svc.record_payout(case, amount=Decimal("10000"), user=self.clerk)
        before = BenevolentNotification.objects.filter(
            event=NotificationEvent.PAYOUT_MADE, case=case).count()
        payout.expense.status = "APPROVED"
        payout.expense.approved_by = self.treasurer
        payout.expense.save()
        self.assertGreater(
            BenevolentNotification.objects.filter(
                event=NotificationEvent.PAYOUT_MADE, case=case).count(), before)

    def test_committee_vote_needed_notifies_every_seated_member_once(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=400),
            user=self.treasurer)
        v2.approval_mode = SchemePolicy.ApprovalMode.COMMITTEE
        v2.committee_quorum = 2
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

        alice = User.objects.create_user("alice7", password="x", email="alice7@x.com")
        bob = User.objects.create_user("bob7", password="x", email="bob7@x.com")
        committee_svc.add_member(self.scheme, alice, added_by=self.treasurer)
        committee_svc.add_member(self.scheme, bob, added_by=self.treasurer)

        m = self._membership()
        case = self._assessed_case(m)
        notified_users = set(BenevolentNotification.objects.filter(
            event=NotificationEvent.COMMITTEE_VOTE_NEEDED, case=case
        ).values_list("user_id", flat=True))
        self.assertEqual(notified_users, {alice.pk, bob.pk})

        # re-assessing (e.g. after a document is added) must NOT spam the
        # committee a second time
        case_svc.assess_case(case, user=self.treasurer)
        self.assertEqual(
            BenevolentNotification.objects.filter(
                event=NotificationEvent.COMMITTEE_VOTE_NEEDED, case=case).count(),
            len(notified_users))

    def test_no_roster_means_no_committee_notification_not_a_crash(self):
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=400),
            user=self.treasurer)
        v2.approval_mode = SchemePolicy.ApprovalMode.COMMITTEE
        v2.committee_quorum = 2
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)
        m = self._membership()
        case = self._assessed_case(m)   # must not raise
        self.assertFalse(BenevolentNotification.objects.filter(
            event=NotificationEvent.COMMITTEE_VOTE_NEEDED, case=case).exists())

    def test_suspending_notifies_the_member(self):
        m = self._membership()
        BenevolentNotification.objects.filter(membership=m).delete()
        reg_svc.suspend(m, user=self.treasurer, reason="Missed dues.")
        n = BenevolentNotification.objects.filter(
            event=NotificationEvent.MEMBERSHIP_STATUS_CHANGED, membership=m).first()
        self.assertIsNotNone(n)

    def test_reinstating_notifies_the_member(self):
        m = self._membership()
        reg_svc.suspend(m, user=self.treasurer, reason="x")
        BenevolentNotification.objects.filter(membership=m).delete()
        reg_svc.reinstate(m, user=self.treasurer)
        self.assertTrue(BenevolentNotification.objects.filter(
            event=NotificationEvent.MEMBERSHIP_STATUS_CHANGED, membership=m).exists())

    def test_a_toggle_switched_off_stops_that_events_notifications(self):
        cfg = BenevolentSettings.get()
        cfg.notify_member_registration = False
        cfg.save()
        m = self._membership()
        self.assertFalse(BenevolentNotification.objects.filter(
            event=NotificationEvent.REGISTRATION_CONFIRMED, membership=m).exists())


# ===========================================================================
# 4. RETRIES
# ===========================================================================

class RetryTests(Phase7Fixture):

    def test_retry_only_touches_failed_rows(self):
        m = self._membership()
        sent = BenevolentNotification.objects.create(
            event="X", channel="SMS", membership=m, recipient="254700000000",
            body="x", status=BenevolentNotification.Status.SENT, attempts=1)
        skipped = BenevolentNotification.objects.create(
            event="X", channel="SMS", membership=m, recipient="254700000000",
            body="x", status=BenevolentNotification.Status.SKIPPED, attempts=1)
        failed = BenevolentNotification.objects.create(
            event="X", channel="SMS", membership=m, recipient="254700000000",
            body="x", status=BenevolentNotification.Status.FAILED, attempts=1)
        notify_svc.retry_failed()
        sent.refresh_from_db(); skipped.refresh_from_db(); failed.refresh_from_db()
        self.assertEqual(sent.attempts, 1)       # untouched
        self.assertEqual(skipped.attempts, 1)    # untouched
        self.assertEqual(failed.attempts, 2)     # retried

    def test_retry_is_bounded_by_max_attempts(self):
        m = self._membership()
        failed = BenevolentNotification.objects.create(
            event="X", channel="SMS", membership=m, recipient="254700000000",
            body="x", status=BenevolentNotification.Status.FAILED, attempts=3)
        retried = notify_svc.retry_failed(max_attempts=3)
        self.assertEqual(retried, 0)
        failed.refresh_from_db()
        self.assertEqual(failed.attempts, 3)

    def test_a_retried_row_that_now_succeeds_becomes_sent(self):
        from unittest import mock
        from core.models import SmsLog
        m = self._membership()
        failed = BenevolentNotification.objects.create(
            event="X", channel="SMS", membership=m, recipient="254700000000",
            body="x", status=BenevolentNotification.Status.FAILED, attempts=1)
        fake_log = SmsLog.objects.create(to="254700000000", message="x",
                                         status=SmsLog.Status.SENT, response="OK")
        with mock.patch("core.services.sms.send_sms", return_value=fake_log):
            notify_svc.retry_failed()
        failed.refresh_from_db()
        self.assertEqual(failed.status, BenevolentNotification.Status.SENT)


# ===========================================================================
# 5. DUE REMINDERS — closes a gap that survived three phases
# ===========================================================================

class DueReminderTests(Phase7Fixture):

    def setUp(self):
        super().setUp()
        v2 = scheme_svc.new_version_from(
            self.policy, effective_from=TODAY - dt.timedelta(days=400),
            user=self.treasurer)
        v2.arrears_treatment = SchemePolicy.ArrearsTreatment.DEDUCT
        v2.save()
        scheme_svc.publish_policy(v2, user=self.treasurer)

    def test_a_member_in_arrears_gets_a_reminder(self):
        m = self._membership(days_ago=200)   # several months of unpaid dues
        self.assertGreater(contrib_svc.arrears_for(m), 0)
        result = notify_svc.send_due_reminders(scheme=self.scheme)
        self.assertEqual(result["arrears"], 1)
        self.assertTrue(BenevolentNotification.objects.filter(
            event=NotificationEvent.ARREARS_REMINDER, membership=m).exists())

    def test_a_member_not_in_arrears_gets_nothing(self):
        m = self._membership(days_ago=200)
        # settle everything owed first, so the ONLY variable under test is
        # "in arrears or not" rather than how many days have passed
        owed = contrib_svc.arrears_for(m)
        if owed:
            contrib_svc.record_contribution(
                self.scheme, date=TODAY, amount=owed, membership=m, user=self.treasurer)
        self.assertEqual(contrib_svc.arrears_for(m), 0)
        result = notify_svc.send_due_reminders(scheme=self.scheme)
        self.assertEqual(result["arrears"], 0)

    def test_the_same_member_is_not_reminded_twice_within_the_gap(self):
        m = self._membership(days_ago=200)
        result1 = notify_svc.send_due_reminders(scheme=self.scheme)
        self.assertEqual(result1["arrears"], 1)
        result2 = notify_svc.send_due_reminders(scheme=self.scheme)   # run again immediately
        self.assertEqual(result2["arrears"], 0)   # throttled, not re-sent

    def test_the_toggle_switches_reminders_off_entirely(self):
        cfg = BenevolentSettings.get()
        cfg.notify_member_arrears_reminder = False
        cfg.save()
        self._membership(days_ago=200)
        result = notify_svc.send_due_reminders(scheme=self.scheme)
        self.assertEqual(result["arrears"], 0)

    def test_a_renewal_due_soon_gets_a_reminder(self):
        cfg = BenevolentSettings.get()
        cfg.renewal_reminder_days = 30
        cfg.save()
        m = self._membership(days_ago=30)
        m.renewed_until = TODAY + dt.timedelta(days=10)   # due imminently
        m.save()
        result = notify_svc.send_due_reminders(scheme=self.scheme)
        self.assertEqual(result["renewal"], 1)
        self.assertTrue(BenevolentNotification.objects.filter(
            event=NotificationEvent.RENEWAL_REMINDER, membership=m).exists())

    def test_a_renewal_far_in_the_future_gets_nothing_yet(self):
        cfg = BenevolentSettings.get()
        cfg.renewal_reminder_days = 30
        cfg.save()
        m = self._membership(days_ago=30)
        m.renewed_until = TODAY + dt.timedelta(days=200)   # nowhere near due
        m.save()
        result = notify_svc.send_due_reminders(scheme=self.scheme)
        self.assertEqual(result["renewal"], 0)

    def test_run_automation_sends_reminders_and_retries_as_part_of_its_cycle(self):
        cfg = BenevolentSettings.get()
        cfg.automation_enabled = True
        cfg.save()
        self._membership(days_ago=200)
        result = scheme_svc.run_automation(force=True)
        self.assertIn("reminders", result)
        self.assertIn("retried", result)
        self.assertIn("arrears", result["summary"].lower())


# ===========================================================================
# 6. FINANCIAL INTEGRATION — confirmed, not rebuilt
# ===========================================================================

class FinancialIntegrationTests(Phase7Fixture):

    def test_a_full_case_lifecycle_still_balances_the_general_ledger(self):
        """Notifications are side effects; they must never be able to affect
        what actually posts. A full contribution → case → payout cycle still
        balances exactly as it did before this phase existed."""
        from ledger.services import posting
        posting.ensure_chart()
        m = self._membership()
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("2000"), membership=m,
            user=self.treasurer)
        case = self._assessed_case(m)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.treasurer,
                              allow_self_approval=True)
        payout = case_svc.record_payout(case, amount=Decimal("10000"), user=self.clerk)
        payout.expense.status = "APPROVED"
        payout.expense.approved_by = self.treasurer
        payout.expense.save()
        self.assertTrue(posting.accounting_equation()["balanced"])

    def test_notification_history_view_and_templates_view_load(self):
        self.client.force_login(self.treasurer)
        for url in [reverse("benevolent_notification_templates"),
                    reverse("benevolent_notification_history")]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_an_assistant_cannot_edit_templates(self):
        tpl = NotificationTemplate.objects.first()
        self.client.force_login(self.clerk)
        r = self.client.get(reverse("benevolent_notification_template_edit", args=[tpl.pk]))
        self.assertNotEqual(r.status_code, 200)
