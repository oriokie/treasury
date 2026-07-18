"""Round 9 follow-up — items 1, 2, 3, 4, 5, 6, 8, 9.

(Item 7 — bank gifts not reaching intake — is tested in
statements/test_benevolent_intake_fund_fallback.py. Item 10 — the three
failing tests — is fixed directly in the affected test files.)
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentContribution,
                               BenevolentEventType, BenevolentScheme,
                               ContributionIntake, SchemeDependant,
                               SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import engine as engine_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import standing as standing_svc

TODAY = dt.date.today()


class Round9Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_r9", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c_r9", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

        self.fund = Department.objects.create(
            name="R9 Fund", slug="r9-fund", fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="R9 Scheme", code="R9S", fund=self.fund, created_by=self.treasurer)
        self.bereavement = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER",
            triggers_on_death=True)

    def _publish_levy_policy(self, **overrides):
        kwargs = dict(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            contribution_amount=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            require_different_approver=True,
            created_by=self.treasurer)
        kwargs.update(overrides)
        policy = SchemePolicy.objects.create(**kwargs)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        if self.scheme.status == BenevolentScheme.Status.DRAFT:
            scheme_svc.activate_scheme(self.scheme, user=self.treasurer)
        return policy

    def _enrol(self, name, days_ago=400, phone=""):
        member = Member.objects.create(name=name, phone=phone)
        return reg_svc.register(
            self.scheme, member, joined_on=TODAY - dt.timedelta(days=days_ago),
            user=self.treasurer)


# ---------------------------------------------------------------------------
# Item 4 — the levy page must not work on a DRAFT case
# ---------------------------------------------------------------------------

class LevyDraftGuardTests(Round9Fixture):
    def test_raise_case_levy_still_works_as_a_pure_calculator_on_draft(self):
        """The roster CALCULATOR itself stays usable for preview purposes —
        only the collection PAGE/action is blocked. Other code (tests,
        obligations engine previews) legitimately calls this on drafts."""
        self._publish_levy_policy()
        m = self._enrol("Payer")
        bereaved = self._enrol("Bereaved")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        self.assertEqual(case.status, BenevolentCase.Status.DRAFT)
        roster = contrib_svc.raise_case_levy(case)   # must not raise
        self.assertIn(m.pk, [r["membership"].pk for r in roster["rows"]])

    def test_levy_page_redirects_away_from_a_draft_case(self):
        self._publish_levy_policy()
        bereaved = self._enrol("Bereaved Page")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(
            reverse("benevolent_case_levy", args=[case.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "draft")
        self.assertNotContains(resp, "Receipt one member")

    def test_levy_post_refuses_a_draft_case(self):
        self._publish_levy_policy()
        payer = self._enrol("POST Payer")
        bereaved = self._enrol("POST Bereaved")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.post(
            reverse("benevolent_case_levy", args=[case.pk]),
            {"membership": payer.pk, "amount": "500"}, follow=True)
        self.assertEqual(
            contrib_svc.levy_paid_by(payer, case), Decimal("0"),
            "a levy payment was recorded against a draft case")

    def test_validate_refuses_a_levy_contribution_against_a_draft_case(self):
        """Defense in depth: the shared validation layer used by manual entry
        and the intake engine also refuses, independent of the page."""
        self._publish_levy_policy()
        payer = self._enrol("Validate Payer")
        bereaved = self._enrol("Validate Bereaved")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        problems = engine_svc.validate(
            self.scheme, kind=BenevolentContribution.Kind.LEVY,
            membership=payer, case=case, amount=Decimal("500"), date=TODAY)
        self.assertTrue(problems)
        self.assertIn("draft", problems[0].lower())

    def test_submitted_case_levy_page_works_normally(self):
        """The fix must not be overbroad — a properly-submitted case's levy
        page works exactly as before."""
        self._publish_levy_policy()
        payer = self._enrol("Normal Payer")
        bereaved = self._enrol("Normal Bereaved")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_case_levy", args=[case.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "btn-sm\">Receipt</button>")


# ---------------------------------------------------------------------------
# Item 6 — intake page: only OPEN cases in the item's OWN scheme
# ---------------------------------------------------------------------------

class IntakeCaseScopingTests(Round9Fixture):
    def test_case_dropdown_excludes_other_schemes(self):
        self._publish_levy_policy()
        other_fund = Department.objects.create(
            name="Other Fund", slug="other-fund", fund_type=Department.FundType.LOCAL)
        other_scheme = BenevolentScheme.objects.create(
            name="Other Scheme", code="OTH", fund=other_fund,
            created_by=self.treasurer)
        other_et = BenevolentEventType.objects.create(
            scheme=other_scheme, name="Ber", code="BER")
        other_policy = SchemePolicy.objects.create(
            scheme=other_scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("5000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(other_policy, user=self.treasurer)
        scheme_svc.activate_scheme(other_scheme, user=self.treasurer)
        other_member = Member.objects.create(name="Other Scheme Bereaved")
        other_m = reg_svc.register(
            other_scheme, other_member, joined_on=TODAY - dt.timedelta(days=400),
            user=self.treasurer)
        other_case = case_svc.create_case(
            other_scheme, event_type=other_et, event_date=TODAY,
            membership=other_m, user=self.treasurer)
        case_svc.submit_case(other_case, user=self.treasurer)

        bereaved = self._enrol("This Scheme Bereaved")
        my_case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(my_case, user=self.treasurer)

        from benevolent.forms import IntakeResolveForm
        from giving.models import Transaction
        txn = Transaction.objects.create(
            date=TODAY, channel=Transaction.Channel.BANK, direction="CREDIT",
            amount=Decimal("500"), department=self.fund,
            allocation_status=Transaction.Status.REVIEW, confirmed=True)
        item = ContributionIntake.objects.create(
            transaction=txn, scheme=self.scheme,
            status=ContributionIntake.Status.REVIEW)
        form = IntakeResolveForm(item=item)
        case_ids = set(form.fields["case"].queryset.values_list("pk", flat=True))
        self.assertIn(my_case.pk, case_ids)
        self.assertNotIn(other_case.pk, case_ids)

    def test_case_dropdown_excludes_closed_cases(self):
        self._publish_levy_policy()
        bereaved = self._enrol("Closed Bereaved")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.clerk)
        case_svc.close_case(case, user=self.treasurer)

        from benevolent.forms import IntakeResolveForm
        from giving.models import Transaction
        txn = Transaction.objects.create(
            date=TODAY, channel=Transaction.Channel.BANK, direction="CREDIT",
            amount=Decimal("500"), department=self.fund,
            allocation_status=Transaction.Status.REVIEW, confirmed=True)
        item = ContributionIntake.objects.create(
            transaction=txn, scheme=self.scheme,
            status=ContributionIntake.Status.REVIEW)
        form = IntakeResolveForm(item=item)
        case_ids = set(form.fields["case"].queryset.values_list("pk", flat=True))
        self.assertNotIn(case.pk, case_ids)

    def test_server_side_refuses_a_closed_case_even_if_posted_directly(self):
        """Defense in depth beyond the form's queryset — a crafted POST must
        also be refused at validate()."""
        self._publish_levy_policy()
        bereaved = self._enrol("Server Side Bereaved")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.clerk)
        case_svc.close_case(case, user=self.treasurer)

        payer = self._enrol("Server Side Payer")
        problems = engine_svc.validate(
            self.scheme, kind=BenevolentContribution.Kind.LEVY,
            membership=payer, case=case, amount=Decimal("100"), date=TODAY)
        self.assertTrue(problems)


# ---------------------------------------------------------------------------
# Item 1 — raise-a-case pre-fills from the deceased member's/dependant's data
# ---------------------------------------------------------------------------

class CasePrefillTests(Round9Fixture):
    def test_membership_query_param_prefills_the_form(self):
        self._publish_levy_policy(bereaved_contribution_policy="EXEMPT")
        m = self._enrol("Prefill Member")
        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(
            reverse("benevolent_case_new", args=[self.scheme.pk])
            + f"?membership={m.pk}")
        form = resp.context["form"]
        self.assertEqual(form.initial.get("membership"), m)
        self.assertEqual(form.initial.get("beneficiary_name"), m.member.name)
        self.assertEqual(form.initial.get("event_date"), TODAY)

    def test_dependant_query_param_prefills_the_form(self):
        self._publish_levy_policy()
        m = self._enrol("Household Head")
        dep = SchemeDependant.objects.create(
            membership=m, name="A Dependant",
            relationship=SchemeDependant.Relationship.CHILD, active=True)
        reg_svc.record_dependant_death(dep, died_on=TODAY, user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(
            reverse("benevolent_case_new", args=[self.scheme.pk])
            + f"?dependant={dep.pk}")
        form = resp.context["form"]
        self.assertEqual(form.initial.get("dependant"), dep)
        self.assertEqual(form.initial.get("beneficiary_name"), dep.display_name)
        self.assertEqual(form.initial.get("event_date"), TODAY)

    def test_household_page_link_passes_dependant_id(self):
        self._publish_levy_policy()
        m = self._enrol("Link Test Head")
        dep = SchemeDependant.objects.create(
            membership=m, name="Link Test Dependant",
            relationship=SchemeDependant.Relationship.SPOUSE, active=True)
        reg_svc.record_dependant_death(dep, died_on=TODAY, user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_membership_detail", args=[m.pk]))
        self.assertContains(resp, f"?dependant={dep.pk}")

    def test_own_death_panel_links_to_existing_case_not_a_duplicate(self):
        """If a case for the member's own death already exists (e.g.
        auto-opened), the panel must link to IT, not offer to raise a
        duplicate."""
        self._publish_levy_policy(bereaved_contribution_policy="EXEMPT")
        m = self._enrol("Existing Case Member")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=m, user=self.treasurer)
        reg_svc.record_death(m, died_on=TODAY, user=self.treasurer)
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_membership_detail", args=[m.pk]))
        self.assertContains(resp, case.number)
        self.assertContains(resp, "Open the case")


# ---------------------------------------------------------------------------
# Item 2 — registration_fee_paid column on the roster bulk import
# ---------------------------------------------------------------------------

class RosterImportRegistrationFeeTests(Round9Fixture):
    def _csv_upload(self, rows):
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile
        header = ["name", "phone", "joined_on", "registration_type",
                 "household_name", "registration_fee_paid", "mark_paid_up"]
        lines = [",".join(header)]
        for r in rows:
            lines.append(",".join(str(r.get(h, "")) for h in header))
        content = "\n".join(lines).encode()
        return SimpleUploadedFile("roster.csv", content, content_type="text/csv")

    def test_registration_fee_paid_column_sets_the_field(self):
        self._publish_levy_policy(registration_required=True,
                                  registration_fee=Decimal("300"))
        f = self._csv_upload([
            {"name": "Paid Already", "phone": "0722000001",
             "joined_on": "2023-01-15", "registration_type": "INDIVIDUAL",
             "registration_fee_paid": "1"},
            {"name": "Not Paid Yet", "phone": "0722000002",
             "joined_on": "2023-01-15", "registration_type": "INDIVIDUAL",
             "registration_fee_paid": "0"},
        ])
        self.client.force_login(self.treasurer)
        self.client.post(
            reverse("benevolent_bulk_import", args=[self.scheme.pk]),
            {"file": f}, follow=True)
        m1 = SchemeMembership.objects.get(member__name="PAID ALREADY")
        m2 = SchemeMembership.objects.get(member__name="NOT PAID YET")
        self.assertTrue(m1.registration_fee_paid)
        self.assertFalse(m2.registration_fee_paid)

    def test_registration_fee_paid_is_independent_of_mark_paid_up(self):
        """The two flags are separate obligations (fee vs dues arrears) and
        must not be conflated."""
        self._publish_levy_policy(registration_required=True,
                                  registration_fee=Decimal("300"))
        f = self._csv_upload([
            {"name": "Fee Only", "phone": "0722000003",
             "joined_on": "2023-01-15", "registration_type": "INDIVIDUAL",
             "registration_fee_paid": "1", "mark_paid_up": "0"},
        ])
        self.client.force_login(self.treasurer)
        self.client.post(
            reverse("benevolent_bulk_import", args=[self.scheme.pk]),
            {"file": f}, follow=True)
        m = SchemeMembership.objects.get(member__name="FEE ONLY")
        self.assertTrue(m.registration_fee_paid)


# ---------------------------------------------------------------------------
# Item 3 — inactivity: consecutive vs rolling-year missed-case counting
# ---------------------------------------------------------------------------

class MissedCaseWindowTests(Round9Fixture):
    def _raise_and_settle(self, days_ago, payer, pay=False, skip_payer=None):
        bereaved = self._enrol(f"Bereaved{days_ago}", days_ago=days_ago + 10)
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement,
            event_date=TODAY - dt.timedelta(days=days_ago),
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.clerk,
                              allow_self_approval=True)
        case_svc.close_case(case, user=self.treasurer)
        if pay and payer is not None:
            contrib_svc.record_contribution(
                self.scheme, date=TODAY - dt.timedelta(days=days_ago), amount=Decimal("500"),
                user=self.treasurer, membership=payer, case=case, channel="CASH")
        return case

    def test_consecutive_mode_is_the_default_and_unchanged(self):
        policy = self._publish_levy_policy(
            inactivity_missed_cases=2,
            inactivity_missed_cases_window=SchemePolicy.MissedCasesWindow.CONSECUTIVE)
        payer = self._enrol("Consecutive Payer", days_ago=900)
        # paid the oldest, missed the two most recent -> consecutive run of 2
        self._raise_and_settle(200, payer, pay=True)
        self._raise_and_settle(100, payer, pay=False)
        self._raise_and_settle(50, payer, pay=False)
        missed = standing_svc.missed_case_levies(payer, policy=policy)
        self.assertEqual(missed, 2)

    def test_rolling_year_counts_non_consecutive_misses(self):
        policy = self._publish_levy_policy(
            inactivity_missed_cases=2,
            inactivity_missed_cases_window=SchemePolicy.MissedCasesWindow.ROLLING_YEAR)
        payer = self._enrol("Rolling Payer", days_ago=900)
        # missed, paid, missed — NOT consecutive, but both misses are within a year
        self._raise_and_settle(200, payer, pay=False)
        self._raise_and_settle(100, payer, pay=True)
        self._raise_and_settle(50, payer, pay=False)
        missed = standing_svc.missed_case_levies(payer, policy=policy)
        self.assertEqual(missed, 2,
                         "rolling-year mode must count both misses even though "
                         "a paid case sits between them")

    def test_rolling_year_excludes_misses_older_than_a_year(self):
        policy = self._publish_levy_policy(
            inactivity_missed_cases=1,
            inactivity_missed_cases_window=SchemePolicy.MissedCasesWindow.ROLLING_YEAR)
        payer = self._enrol("Old Miss Payer", days_ago=900)
        self._raise_and_settle(400, payer, pay=False)   # older than 365 days
        missed = standing_svc.missed_case_levies(payer, policy=policy, as_of=TODAY)
        self.assertEqual(missed, 0)

    def test_field_is_frozen_into_the_policy_version(self):
        """A genuine RULE_FIELDS regression check: the window setting must be
        part of terms_snapshot(), or a case's basis could silently change
        after the fact."""
        policy = self._publish_levy_policy(
            inactivity_missed_cases_window=SchemePolicy.MissedCasesWindow.ROLLING_YEAR)
        self.assertIn("inactivity_missed_cases_window", SchemePolicy.RULE_FIELDS)
        snap = policy.terms_snapshot()
        self.assertEqual(snap.get("inactivity_missed_cases_window"), "ROLLING_YEAR")


# ---------------------------------------------------------------------------
# Item 5 — configurable create-and-approve-in-one-step
# ---------------------------------------------------------------------------

class CreateAndApproveTests(Round9Fixture):
    def test_field_not_offered_when_policy_requires_different_approver(self):
        self._publish_levy_policy(require_different_approver=True)
        from benevolent.forms import CaseForm
        form = CaseForm(scheme=self.scheme)
        self.assertNotIn("create_and_approve", form.fields)

    def test_field_offered_when_policy_allows_self_approval(self):
        self._publish_levy_policy(require_different_approver=False)
        from benevolent.forms import CaseForm
        form = CaseForm(scheme=self.scheme)
        self.assertIn("create_and_approve", form.fields)

    def test_one_step_create_and_approve(self):
        self._publish_levy_policy(require_different_approver=False,
                                  bereaved_contribution_policy="EXEMPT")
        bereaved = self._enrol("Fast Path Bereaved")
        self.treasurer.groups.add(  # needs the Approve right too
            Group.objects.get_or_create(name="Benevolent Approver")[0])
        from core.roles import can_approve_benevolent
        # ensure the treasurer group itself already carries approve rights
        self.assertTrue(can_approve_benevolent(self.treasurer))

        self.client.force_login(self.treasurer)
        resp = self.client.post(
            reverse("benevolent_case_new", args=[self.scheme.pk]), {
                "membership": bereaved.pk, "event_type": self.bereavement.pk,
                "event_date": TODAY.isoformat(), "reported_date": TODAY.isoformat(),
                "beneficiary_name": bereaved.member.name,
                "create_and_approve": "1",
            }, follow=True)
        self.assertEqual(resp.status_code, 200)
        case = BenevolentCase.objects.filter(membership=bereaved).first()
        self.assertIsNotNone(case)
        self.assertEqual(case.status, BenevolentCase.Status.APPROVED)
        self.assertIsNotNone(case.approved_amount)

    def test_without_the_checkbox_case_stays_a_draft(self):
        self._publish_levy_policy(require_different_approver=False,
                                  bereaved_contribution_policy="EXEMPT")
        bereaved = self._enrol("Draft Path Bereaved")
        self.client.force_login(self.treasurer)
        self.client.post(
            reverse("benevolent_case_new", args=[self.scheme.pk]), {
                "membership": bereaved.pk, "event_type": self.bereavement.pk,
                "event_date": TODAY.isoformat(), "reported_date": TODAY.isoformat(),
                "beneficiary_name": bereaved.member.name,
            }, follow=True)
        case = BenevolentCase.objects.filter(membership=bereaved).first()
        self.assertIsNotNone(case)
        self.assertEqual(case.status, BenevolentCase.Status.DRAFT)


# ---------------------------------------------------------------------------
# Items 8/9 — the SMS Center
# ---------------------------------------------------------------------------

class SmsCenterTests(Round9Fixture):
    def test_all_active_audience(self):
        self._publish_levy_policy()
        self._enrol("Active One", phone="0722000010")
        self._enrol("Active Two", phone="0722000011")
        from benevolent.services import bulk_sms
        recipients = bulk_sms.audience_all_active(self.scheme)
        self.assertEqual(len(recipients), 2)

    def test_defaulters_audience_under_levy_policy(self):
        policy = self._publish_levy_policy()
        payer = self._enrol("Will Pay", phone="0722000020")
        defaulter = self._enrol("Wont Pay", phone="0722000021")
        bereaved = self._enrol("Case Subject", phone="0722000022")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.clerk,
                              allow_self_approval=True)
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), user=self.treasurer,
            membership=payer, case=case, channel="CASH")

        from benevolent.services import bulk_sms
        recipients = bulk_sms.audience_defaulters(self.scheme)
        member_ids = {m.pk for m, _ in recipients}
        self.assertIn(defaulter.pk, member_ids)
        self.assertNotIn(payer.pk, member_ids)

    def test_case_roster_audience_matches_the_levy_page(self):
        self._publish_levy_policy()
        payer = self._enrol("Roster Payer", phone="0722000030")
        owes = self._enrol("Roster Owes", phone="0722000031")
        bereaved = self._enrol("Roster Bereaved", phone="0722000032")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        contrib_svc.record_contribution(
            self.scheme, date=TODAY, amount=Decimal("500"), user=self.treasurer,
            membership=payer, case=case, channel="CASH")

        from benevolent.services import bulk_sms
        recipients = bulk_sms.audience_case_roster_unpaid(case)
        member_ids = {m.pk for m, _ in recipients}
        self.assertIn(owes.pk, member_ids)
        self.assertNotIn(payer.pk, member_ids)

    def test_send_bulk_sms_logs_and_substitutes_placeholders(self):
        self._publish_levy_policy()
        m = self._enrol("Substitution Test", phone="0722000040")
        from benevolent.services import bulk_sms
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.sms_enabled = False   # disabled -> logged as DISABLED, never raises
        cfg.save()
        result = bulk_sms.send_bulk_sms(
            [(m, "test")], "Dear {name}, from {scheme}.", scheme=self.scheme)
        self.assertEqual(len(result.logs), 1)
        self.assertIn("Substitution", result.logs[0].message)
        self.assertIn("R9 Scheme", result.logs[0].message)

    def test_member_with_no_phone_is_skipped_not_failed(self):
        self._publish_levy_policy()
        m = self._enrol("No Phone", phone="")
        from benevolent.services import bulk_sms
        result = bulk_sms.send_bulk_sms(
            [(m, "test")], "Hi {name}", scheme=self.scheme)
        self.assertEqual(result.no_phone, 1)
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.failed, 0)

    def test_sms_center_view_requires_manage_right(self):
        self._publish_levy_policy()
        outsider = User.objects.create_user("outsider_r9", password="x")
        self.client.force_login(outsider)
        resp = self.client.get(reverse("benevolent_sms_center", args=[self.scheme.pk]))
        self.assertNotEqual(resp.status_code, 200)

    def test_sms_center_page_renders_with_audiences(self):
        self._publish_levy_policy()
        self._enrol("Render Test", phone="0722000050")
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_sms_center", args=[self.scheme.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "All active members")

    def test_sms_center_post_sends_to_audience(self):
        self._publish_levy_policy()
        self._enrol("Send Test", phone="0722000060")
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.sms_enabled = False
        cfg.save()
        self.client.force_login(self.treasurer)
        resp = self.client.post(
            reverse("benevolent_sms_center", args=[self.scheme.pk]),
            {"audience": "ALL_ACTIVE", "message": "Hello {name}"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        from core.models import SmsLog
        self.assertTrue(SmsLog.objects.filter(message__icontains="Hello").exists())

    def test_notify_members_link_appears_on_approved_case(self):
        self._publish_levy_policy(bereaved_contribution_policy="EXEMPT")
        bereaved = self._enrol("Notify Link Bereaved")
        case = case_svc.create_case(
            self.scheme, event_type=self.bereavement, event_date=TODAY,
            membership=bereaved, user=self.treasurer)
        case_svc.submit_case(case, user=self.treasurer)
        case_svc.assess_case(case, user=self.treasurer)
        case_svc.approve_case(case, amount=Decimal("10000"), user=self.clerk,
                              allow_self_approval=True)
        self.client.force_login(self.treasurer)
        resp = self.client.get(reverse("benevolent_case_detail", args=[case.pk]))
        self.assertContains(resp, "Notify members")
