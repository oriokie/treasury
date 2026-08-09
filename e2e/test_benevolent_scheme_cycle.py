"""The welfare scheme, from an enrolment form to a benefit in a family's hands.

A church runs its benevolent scheme end to end: it writes the constitution
(a scheme, its qualifying events, a versioned policy) and opens it; a member
enrols and is admitted; they pay their monthly dues and stand in good standing;
a bereavement is reported for a member of their household; the case is
submitted, assessed against the policy that was in force on the day of the
death, approved by the person the policy says may approve it, and paid through
the ordinary payment voucher — which the treasury still has to approve, because
this module never approves its own money. Then the family's own side of it: the
member is invited to the portal, sets a password, signs in, and reads their own
case, their own dues and their own standing.

Every one of those steps has unit tests. What none of them can show is that the
figures still agree at the END: that the fund fell by exactly the benefit and by
nothing else, that a member in arrears was paid exactly what the policy says
they are owed less exactly what they owe, and that the member reading their own
record on a phone sees the same numbers the treasurer sees on a desktop.

Two failure shapes this file exists for in particular:

* **#125 — the page that renders only on an empty record.** The portal is
  excluded from the seeded smoke sweep, and its pages have shipped rendering
  perfectly for a member invited five minutes ago and blowing up for a member
  who actually has something. So the portal is walked here with a member who has
  a case, a dues schedule, a dependant and a payout.
* **The seam between two steps.** The benefit is decided in one module and paid
  in another; the arrears deducted from it are computed as at the event date by
  one service and displayed as at today by another. Those are the joins where
  this application's money has gone wrong before.
"""
import datetime as dt
import re
from decimal import Decimal
from unittest import mock

from django.contrib.messages import constants as message_levels
from django.urls import reverse

from benevolent.models import (BenevolentCase, BenevolentScheme, MemberAccount,
                               SchemeDependant, SchemeMembership, SchemePolicy,
                               Standing)
from cashbook.models import Expense
from core.models import SiteConfig
from departments.models import Department
from members.models import Member

from .base import BusinessWorkflowTest, WorkflowError

#: Everything in this file is dated relative to the real today, on purpose. A
#: welfare scheme's arrears, standing and dues schedule are all computed as at
#: `date.today()` deep inside the services, so a workflow pinned to a fixed
#: calendar date would quietly drift into arrears as the months passed and this
#: suite would start failing for the wrong reason.
TODAY = dt.date.today()

DUES = Decimal("500")                 # per member, per month
BENEFIT = Decimal("50000")            # the fixed bereavement benefit
OPENING_GIFT = Decimal("80000")       # what a benefactor put into the fund
PORTAL_PASSWORD = "member-portal-pass-9"


def money(value):
    """Two decimal places.

    `assert_agree` compares the *string* form of each Decimal, so a figure that
    came back from an aggregate as 33000.00 and the same figure computed in
    Python as 33000 are reported as a disagreement when they are the same money.
    Everything handed to `assert_agree` in this file goes through here first, so
    a failure there is a real difference and not a difference of scale.
    """
    return Decimal(value or 0).quantize(Decimal("0.01"))


def month_start(months_back):
    """The first of the month `months_back` months before this one."""
    year, month = TODAY.year, TODAY.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return dt.date(year, month, 1)


#: Cover begins six dues periods ago (this month included), so a member who has
#: paid every month owes nothing today and one who stopped after two months owes
#: a figure that can be stated exactly.
COVER_FROM = month_start(5)
#: The bereavement: the 10th of the month before last. Always at least 98 days
#: after COVER_FROM, so the policy's 90-day waiting period is genuinely served.
EVENT_DATE = month_start(2) + dt.timedelta(days=9)
REPORTED_DATE = EVENT_DATE + dt.timedelta(days=3)
PAYOUT_DATE = EVENT_DATE + dt.timedelta(days=20)


class WelfareSchemeCycle(BusinessWorkflowTest):
    """A church, a welfare scheme, and one bereavement paid from beginning to end."""

    # ------------------------------------------------------------------
    # Background: the church, its officers, and a constitution written
    # through the app's own setup screens.
    # ------------------------------------------------------------------

    def setUp(self):
        super().setUp()

        # The SMS transport is the one genuinely external thing in this
        # workflow (the portal invitation is taken up via the app's own
        # forgot-password flow, which texts a one-time code). Patched, not
        # skipped: the code the member types is read back out of the message
        # the application actually tried to send, so the flow is walked, not
        # simulated.
        self.sent_messages = []
        patcher = mock.patch("core.services.sms.send_sms",
                             side_effect=self._capture_sms)
        patcher.start()
        self.addCleanup(patcher.stop)

        cfg = SiteConfig.get()
        cfg.require_expense_approval = True      # the strict, default path
        cfg.sms_enabled = True                   # so "forgot password" texts a code
        cfg.sms_api_key = "workflow-key"
        cfg.sms_partner_id = "workflow-partner"
        cfg.sms_shortcode = "CHURCH"
        cfg.save()

        # The treasurer writes the rules and authorises money. The welfare clerk
        # runs the scheme day to day. Two people, because segregation of duties
        # is a rule this policy actually turns on.
        self.office = self.acting_as(self.treasurer)
        self.clerk_user = self.assistant
        self.clerk = self.acting_as(self.clerk_user)

        # The scheme's own fund. Background, like the church's other funds — the
        # welfare workflow does not create funds, it spends out of one.
        self.welfare_fund = Department.objects.create(
            name="Benevolent Fund", slug="wf-benevolent",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)

        # People already on the church roll. Enrolling them into the scheme is
        # the workflow; being known to the church is not.
        self.grace = Member.objects.create(name="Grace Wanjiru", phone="254711000111")
        self.peter = Member.objects.create(name="Peter Otieno", phone="254711000222")
        self.benefactor = Member.objects.create(name="Josiah Kimani",
                                                phone="254711000333")

        self._write_the_constitution()
        self._take_the_opening_gift()

    def _capture_sms(self, to, message, cfg=None):
        self.sent_messages.append((to, message))
        return None

    # -- setup, walked through the screens a treasurer actually uses ----------

    def _write_the_constitution(self):
        """Scheme, qualifying event, policy, publication, activation."""
        # 1. the treasurer creates the scheme and names the fund that holds it
        self.submit(self.office, "benevolent_scheme_new", {
            "name": "Bereavement Benevolent Scheme", "code": "BEN",
            "kind": BenevolentScheme.Kind.BENEVOLENT,
            "fund": self.welfare_fund.id,
            "description": "Stands with a family at a funeral."})
        self.scheme = BenevolentScheme.objects.get(code="BEN")

        # 2. the scheme's vocabulary of qualifying events
        self.submit(self.office, "benevolent_event_types", {
            "name": "Bereavement", "code": "BER",
            "description": "A death in the member's household.",
            "covers_dependants": "on", "triggers_on_death": "on",
            "sort_order": "0", "active": "on"}, args=[self.scheme.pk])
        self.bereavement = self.scheme.event_types.get(code="BER")

        # 3. the constitution itself, drafted on the policy screen
        self.submit(self.office, "benevolent_policy_new",
                    self._policy_payload(), args=[self.scheme.pk])
        self.policy = SchemePolicy.objects.get(scheme=self.scheme, version=1)
        self.assertEqual(
            self.policy.status, SchemePolicy.Status.DRAFT,
            "a policy must start as a draft — the rules are not in force until "
            "somebody publishes them")

        # 4. published: from here the rules are permanent and versioned
        self.submit(self.office, "benevolent_policy_action", {},
                    args=[self.scheme.pk, self.policy.pk, "publish"])
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.status, SchemePolicy.Status.ACTIVE,
                         "the policy did not publish, so nothing can be assessed")

        # 5. and the scheme opened
        self.submit(self.office, "benevolent_scheme_action", {},
                    args=[self.scheme.pk, "activate"])
        self.scheme.refresh_from_db()
        self.assertEqual(self.scheme.status, BenevolentScheme.Status.ACTIVE)

    def _policy_payload(self, **overrides):
        """Every rule on the policy form, as a treasurer would fill it in.

        The form carries the whole constitution (60-odd fields) and refuses a
        partial POST, so this is spelled out rather than derived — a payload
        built from model defaults would silently stop covering a field the day
        someone added one.
        """
        data = {
            "effective_from": COVER_FROM.isoformat(),

            # membership & eligibility
            "membership_required": "on",
            "waiting_period_days": "90",
            "min_contributions": "1",
            "min_paid_months": "0",
            "missed_contributions_allowed": "0",
            "arrears_treatment": SchemePolicy.ArrearsTreatment.DEDUCT,
            "max_arrears_allowed": "0",
            "max_arrears_periods": "0",
            "catch_up_restores_eligibility": "on",
            "catch_up_requalify_days": "0",
            "grace_period_days": "0",

            # registration — a treasurer admits, so enrolment is a two-step act
            "registration_required": "on",
            "registration_approval": SchemePolicy.RegistrationApproval.TREASURER,
            "registration_fee": "0",
            "min_age": "0", "max_age": "0", "exemption_age": "0",

            # renewals: none
            "renewal_period": SchemePolicy.RenewalPeriod.NONE,
            "renewal_fee": "0", "renewal_month": "1", "renewal_grace_days": "30",

            # contributions & funding
            "contribution_mode": SchemePolicy.ContributionMode.FIXED_PERIODIC,
            "contribution_amount": str(DUES),
            "contribution_frequency": SchemePolicy.Frequency.MONTHLY,
            "levy_amount": "0", "max_levies_per_year": "0", "joining_fee": "0",
            "funding_methods": ["DUES", "DONATION"],

            # the benefit
            "benefit_mode": SchemePolicy.BenefitMode.FIXED,
            "benefit_amount": str(BENEFIT),
            "benefit_percent": "0",
            "benefit_cap": "", "benefit_floor": "",
            "benefit_rounding": SchemePolicy.Rounding.NONE,

            # approval: a treasurer, and never the person who raised the case
            "approval_mode": SchemePolicy.ApprovalMode.TREASURER,
            "committee_threshold": "0", "committee_quorum": "3",
            "require_different_approver": "on",

            # the bereaved member's own contribution
            "bereaved_contribution_policy":
                SchemePolicy.BereavedContributionPolicy.EXEMPT,
            "bereaved_reduction_percent": "50",
            "bereaved_dues_waiver_months": "0",

            # inactivity
            "inactivity_months": "0", "inactivity_missed_cases": "0",
            "inactivity_missed_cases_window": "ROLLING_YEAR",
            "inactivity_action": SchemePolicy.InactivityAction.NONE,
            "reinstatement_fee": "0", "reinstatement_waiting_days": "0",

            # household & dependants
            "household_mode": SchemePolicy.HouseholdMode.HOUSEHOLD,
            "max_dependants": "0", "dependant_age_limit": "0",
            "max_household_size": "0", "spouse_auto_covered": "on",

            # on a member's death
            "inheritance_mode": SchemePolicy.InheritanceMode.NONE,
            "refund_percent": "0",

            # claims
            "claim_window_days": "90", "max_claims_per_year": "0",
            "max_benefit_per_year": "0",
            "allow_override": "on", "allow_exemptions": "on", "allow_transfers": "on",

            "notes": "Adopted by the church board.",
        }
        data.update(overrides)
        return data

    def _take_the_opening_gift(self):
        """A benefactor's gift, receipted through the contribution screen — the
        fund has to have something in it before a benefit can come out of it,
        and it gets there the way it really does."""
        self.submit(self.clerk, "benevolent_contribute", {
            "member": self.benefactor.id,
            "date": COVER_FROM.isoformat(),
            "amount": str(OPENING_GIFT),
            "channel": "BANK",
            "payer_type": "SELF",
            "note": "Opening gift to the benevolent fund",
        }, args=[self.scheme.pk])

    # ------------------------------------------------------------------
    # Helpers: each one is a step of the business process, through HTTP
    # ------------------------------------------------------------------

    def _no_error_message(self, response, what):
        """Fail if the view flashed an error and redirected.

        Half the benevolent module's write views do not re-render a bound form
        on failure — they `messages.error(...)` and redirect, which `submit()`
        cannot see (there is no form on the response to carry the errors). This
        is the same guarantee for those views: a step that announced its own
        failure did not happen.
        """
        problems = [m.message for m in response.context.get("messages", [])
                    if m.level >= message_levels.ERROR]
        if problems:
            raise WorkflowError(
                f"{what} was refused and changed nothing:\n  " + "\n  ".join(problems))
        return response

    def _enrol(self, member, spouse_name="", joined_on=None):
        """The registration form: a household enrolment, with the spouse on it."""
        joined_on = joined_on or COVER_FROM
        response = self.submit(self.clerk, "benevolent_register", {
            "member": member.id,
            "registration_type": "HOUSEHOLD",
            "household_name": f"The {member.name.split()[-1]} household",
            "joined_on": joined_on.isoformat(),
            "spouse_name": spouse_name,
            "notes": "Enrolled at the district meeting.",
        }, args=[self.scheme.pk])
        self._no_error_message(response, f"Registering {member.name}")
        return SchemeMembership.objects.get(scheme=self.scheme, member=member)

    def _admit(self, membership, on=None):
        response = self.submit(self.clerk, "benevolent_membership_admin", {
            "on": (on or COVER_FROM).isoformat(),
            "reason": "Application accepted by the welfare committee.",
        }, args=[membership.pk, "admit"])
        self._no_error_message(response, f"Admitting {membership.member.name}")
        membership.refresh_from_db()
        return membership

    def _add_dependant(self, membership, name, relationship, date_of_birth=None,
                       registered_on=None):
        response = self.submit(self.clerk, "benevolent_household", {
            "name": name,
            "relationship": relationship,
            "registered_on": (registered_on or COVER_FROM).isoformat(),
            "date_of_birth": date_of_birth.isoformat() if date_of_birth else "",
        }, args=[membership.pk])
        self._no_error_message(response, f"Adding {name} to the household")
        return SchemeDependant.objects.get(membership=membership, name=name)

    def _pay_dues(self, membership, months_back):
        """One month's dues, receipted on the first of the month it covers."""
        for offset in months_back:
            on = month_start(offset)
            self.submit(self.clerk, "benevolent_contribute", {
                "membership": membership.id,
                "date": on.isoformat(),
                "amount": str(DUES),
                "channel": "CASH",
                "payer_type": "SELF",
                "period_label": f"{on.year}-{on.month:02d}",
                "note": "Monthly dues",
            }, args=[self.scheme.pk])

    def _raise_case(self, membership, dependant, event_date=None):
        event_date = event_date or EVENT_DATE
        response = self.submit(self.clerk, "benevolent_case_new", {
            "dependant": dependant.pk,
            "membership": membership.pk,
            "event_type": self.bereavement.pk,
            "beneficiary_name": dependant.display_name,
            "beneficiary_relationship": "",
            "event_date": event_date.isoformat(),
            "reported_date": (event_date + dt.timedelta(days=3)).isoformat(),
            "claimed_amount": str(BENEFIT),
            "description": "Burial expenses for the member's household.",
        }, args=[self.scheme.pk])
        self._no_error_message(response, "Raising the case")
        return BenevolentCase.objects.filter(
            scheme=self.scheme, membership=membership,
            dependant=dependant).order_by("-id").first()

    def _case_action(self, case, action, data=None, client=None):
        response = self.submit(client or self.clerk, "benevolent_case_action",
                               data or {}, args=[case.pk, action])
        self._no_error_message(response, f"'{action}' on {case.number}")
        case.refresh_from_db()
        return case

    def _approve_case(self, case, amount, client=None):
        response = self.submit(client or self.office, "benevolent_case_decide",
                               {"amount": str(amount)}, args=[case.pk, "approve"])
        self._no_error_message(response, f"Approving {case.number}")
        case.refresh_from_db()
        return case

    def _raise_voucher(self, case, amount, date=None):
        response = self.submit(self.clerk, "benevolent_case_payout", {
            "amount": str(amount),
            "date": (date or PAYOUT_DATE).isoformat(),
            "payee_name": "",
            "method": Expense.Method.BANK,
            "voucher_no": f"BEN-{case.pk}",
            "note": "Benefit paid to the family.",
        }, args=[case.pk])
        self._no_error_message(response, f"Raising the voucher on {case.number}")
        case.refresh_from_db()
        payout = case.payouts.order_by("-id").first()
        if payout is None or payout.expense_id is None:
            raise WorkflowError(
                f"No payment voucher was raised on {case.number}, so nothing "
                f"will ever pay the family.")
        return payout

    def _approve_voucher(self, expense):
        """The ordinary expense queue. The welfare module raises the voucher;
        the treasury approves it, exactly as for any other claim."""
        self.submit(self.office, "expense_approve", {"action": "approve"},
                    args=[expense.pk])
        expense.refresh_from_db()
        if expense.status != Expense.Status.APPROVED:
            raise WorkflowError(
                f"The benefit voucher is {expense.get_status_display().lower()} "
                f"after the treasurer approved it — the money has not moved.")
        return expense

    # -- money readings -------------------------------------------------------

    def _fund_closing(self, fund=None, as_of=None):
        """The fund's closing balance as the fund summary reports it.

        `assert_fund_balance` asserts against this; this returns the figure, so
        it can be put beside the other two ways the same balance is computed.
        """
        from reports.services import balances
        fund = fund or self.welfare_fund
        for row in balances.department_summary(None, as_of or TODAY):
            if getattr(row.get("department", None), "id", None) == fund.id:
                return Decimal(row.get("closing") or 0)
        raise WorkflowError(f"{fund.name} does not appear in the fund summary at all.")

    # -- the whole cycle, for the tests that need one already run -------------

    def _grace_enrolled_and_paid_up(self):
        """Grace: enrolled, admitted, a spouse and a child on her household, and
        every month's dues paid. Returns her membership."""
        membership = self._enrol(self.grace, spouse_name="Samuel Wanjiru")
        self._admit(membership)
        self._add_dependant(membership, "Mary Wanjiru", SchemeDependant.Relationship.CHILD,
                            date_of_birth=dt.date(TODAY.year - 12, 3, 4))
        self._pay_dues(membership, range(5, -1, -1))
        return membership

    def _a_paid_bereavement(self):
        """The whole cycle once, for the tests that start after it. Returns
        (membership, case)."""
        membership = self._grace_enrolled_and_paid_up()
        spouse = membership.dependants.get(relationship=SchemeDependant.Relationship.SPOUSE)
        case = self._raise_case(membership, spouse)
        self._case_action(case, "submit")
        self._case_action(case, "assess")
        self._approve_case(case, case.assessed_amount)
        payout = self._raise_voucher(case, case.approved_amount)
        self._approve_voucher(payout.expense)
        case.refresh_from_db()
        self._case_action(case, "close")
        return membership, case

    # ==================================================================
    # 1. The whole process, and the money at the end of it
    # ==================================================================

    def test_a_bereavement_is_paid_and_the_fund_falls_by_exactly_the_benefit(self):
        # 1. the welfare clerk registers Grace and her husband as a household.
        #    The policy says a treasurer admits, so she starts PENDING and is
        #    NOT yet covered.
        membership = self._enrol(self.grace, spouse_name="Samuel Wanjiru")
        self.assertEqual(
            membership.status, SchemeMembership.Status.PENDING,
            "the policy requires formal admission, so a new enrolment must wait")
        self.assertIsNone(membership.registered_on,
                          "cover cannot run from a date nobody admitted them on")

        # 2. she is admitted. Cover — and the 90-day waiting period — runs from
        #    here, not from the day her name was typed into a list.
        self._admit(membership)
        self.assertEqual(membership.status, SchemeMembership.Status.ACTIVE)
        self.assertEqual(membership.cover_from, COVER_FROM)

        # 3. their daughter is added to the household record
        self._add_dependant(membership, "Mary Wanjiru",
                            SchemeDependant.Relationship.CHILD,
                            date_of_birth=dt.date(TODAY.year - 12, 3, 4))
        self.assertEqual(membership.dependants.filter(active=True).count(), 2,
                         "the spouse from the enrolment form and the daughter")

        # 4. she pays every month's dues. The fund holds the gift plus her dues.
        self._pay_dues(membership, range(5, -1, -1))
        dues_paid = DUES * 6
        self.assert_fund_balance(self.welfare_fund, OPENING_GIFT + dues_paid,
                                 as_of=TODAY)

        # 5. and she is therefore in good standing, owing nothing. This is the
        #    LIVE assessment — the pure function that owns the answer. (The
        #    cached column beside it does not agree; see the expected failure
        #    `test_a_paid_up_member_is_not_told_she_is_in_arrears`.)
        from benevolent.services.contributions import arrears_for
        from benevolent.services.standing import assess
        membership.refresh_from_db()
        live = assess(membership)
        self.assertEqual(
            live.standing, Standing.GOOD,
            f"a member who has paid all six months is not in good standing: "
            f"{live.reason}")
        self.assertEqual(arrears_for(membership, self.policy), Decimal(0))

        # 6. her husband dies. The clerk raises the case for the registered
        #    dependant; the member and the relationship come off his record.
        spouse = membership.dependants.get(
            relationship=SchemeDependant.Relationship.SPOUSE)
        case = self._raise_case(membership, spouse)
        self.assertEqual(case.status, BenevolentCase.Status.DRAFT)
        self.assertEqual(case.membership_id, membership.pk)

        # 7. submitted, then assessed against the policy in force ON THE DAY OF
        #    THE DEATH — which is the whole reason policies are versioned
        self._case_action(case, "submit")
        self._case_action(case, "assess")
        self.assertEqual(case.status, BenevolentCase.Status.ASSESSED)
        self.assertEqual(case.policy_id, self.policy.pk)
        self.assertTrue(case.eligible,
                        f"a paid-up member's claim was assessed as ineligible: "
                        f"{[c['label'] for c in case.failed_checks]}")
        self.assertEqual(
            case.assessed_amount, BENEFIT,
            "a paid-up member under a fixed-benefit policy is entitled to the "
            "whole benefit, with nothing deducted")

        # 8. nothing has moved yet. An assessed case is a decision waiting to be
        #    made, not money spent — this is the half that goes wrong quietly.
        self.assert_fund_balance(self.welfare_fund, OPENING_GIFT + dues_paid,
                                 as_of=TODAY)

        # 9. the treasurer approves it — the clerk who raised it may not, and
        #    that is the policy's own rule (require_different_approver)
        self._approve_case(case, case.assessed_amount)
        self.assertEqual(case.status, BenevolentCase.Status.APPROVED)
        self.assertEqual(case.approved_amount, BENEFIT)
        self.assertEqual(case.approved_by_id, self.treasurer.pk)

        # 10. approving the CASE still moves no money: it authorises a benefit,
        #     it does not pay one. The commitment is visible; the cash is not gone.
        self.assert_fund_balance(self.welfare_fund, OPENING_GIFT + dues_paid,
                                 as_of=TODAY)
        from benevolent.services import reporting as report_svc
        self.assertEqual(report_svc.approved_unpaid_total(self.scheme), BENEFIT,
                         "an approved, unpaid benefit is a commitment and should "
                         "be reported as one")

        # 11. the clerk raises the payment voucher. It enters the ORDINARY
        #     expense queue as PENDING — the welfare module never approves its
        #     own payments.
        payout = self._raise_voucher(case, case.approved_amount)
        self.assertEqual(payout.expense.status, Expense.Status.PENDING)
        self.assertEqual(payout.expense.department_id, self.welfare_fund.id)
        self.assertEqual(payout.expense.category, Expense.Category.BENEVOLENCE)
        self.assert_fund_balance(self.welfare_fund, OPENING_GIFT + dues_paid,
                                 as_of=TODAY)

        # 12. the treasurer approves the voucher in the cash book, and only NOW
        #     does the fund fall — by exactly the benefit and by nothing else
        self._approve_voucher(payout.expense)
        self.assert_fund_balance(self.welfare_fund,
                                 OPENING_GIFT + dues_paid - BENEFIT, as_of=TODAY)

        # 13. the case follows the voucher automatically: it is PAID because the
        #     document that carries the money says so
        case.refresh_from_db()
        self.assertEqual(case.status, BenevolentCase.Status.PAID)
        self.assertEqual(case.paid_total, BENEFIT)
        self.assertEqual(case.outstanding, Decimal(0))

        # 14. and the clerk closes it
        self._case_action(case, "close")
        self.assertEqual(case.status, BenevolentCase.Status.CLOSED)

        # -- the invariants ------------------------------------------------
        self.assert_books_balance("after paying a benevolent benefit")
        self.assert_trial_balance_balances()

    def test_the_fund_reads_the_same_however_it_is_assembled(self):
        """One benefit, four readings.

        The scheme's balance, the fund summary's closing figure, income less
        expenditure on that fund, and the arithmetic of what went in and out.
        This module's whole accounting claim is that it invents no money maths
        of its own — which is only true if these agree.
        """
        membership, case = self._a_paid_bereavement()
        dues_paid = DUES * 6

        from benevolent.services import reporting as report_svc
        received = report_svc.contributions_total(scheme=self.scheme)
        paid_out = report_svc.payouts_total(scheme=self.scheme)

        self.assert_agree(
            "the benevolent fund after one benefit, read four ways",
            scheme_balance=money(report_svc.scheme_balance(self.scheme)),
            fund_summary_closing=money(self._fund_closing()),
            received_less_paid=money(received - paid_out),
            gift_plus_dues_less_benefit=money(OPENING_GIFT + dues_paid - BENEFIT),
        )
        self.assert_agree(
            "what the scheme received",
            fund_income=money(received),
            gift_plus_dues=money(OPENING_GIFT + dues_paid),
        )
        self.assert_agree(
            "what the scheme paid out",
            fund_expenditure=money(paid_out),
            case_paid_total=money(case.paid_total),
            the_benefit=money(BENEFIT),
        )
        self.assert_books_balance("after reading the fund four ways")

    def test_the_pages_the_workflow_ends_on_actually_open(self):
        """Where a treasurer lands when the case is done.

        A benefit that is decided and paid and then cannot be looked at has not
        been dealt with, and that is the specific failure this application has
        shipped five times.
        """
        membership, case = self._a_paid_bereavement()

        for name, args in [
            ("benevolent_dashboard", []),
            ("benevolent_scheme_list", []),
            ("benevolent_scheme_detail", [self.scheme.pk]),
            ("benevolent_case_list", []),
            ("benevolent_case_detail", [case.pk]),
            ("benevolent_case_statement", [case.pk]),
            ("benevolent_registry", []),
            ("benevolent_membership_list", []),
            ("benevolent_membership_detail", [membership.pk]),
            ("benevolent_contribution_list", []),
            ("benevolent_fund_position", [self.scheme.pk]),
            ("expense_detail", [case.payouts.first().expense_id]),
        ]:
            with self.subTest(page=name):
                self.visit(self.office, name, args=args)

        # the case page must actually carry the decision, not merely render
        case_page = self.visit(self.office, "benevolent_case_detail", args=[case.pk])
        body = case_page.content.decode()
        self.assertIn(case.number, body)
        self.assertIn("50,000", body,
                      "the case page does not show the benefit that was paid")

        # and the balance a treasurer reads on the scheme screen has to be the
        # balance the fund summary reports. Two screens, two code paths, one
        # fund — this is the seam a per-view test cannot see.
        scheme_page = self.visit(self.office, "benevolent_scheme_detail",
                                 args=[self.scheme.pk])
        self.assert_agree(
            "the welfare fund, on the scheme screen and on the fund summary",
            scheme_screen=money(scheme_page.context["balance"]),
            case_screen=money(case_page.context["fund_balance"]),
            fund_summary=money(self._fund_closing()),
        )
        self.assert_agree(
            "what the scheme screen says it paid out",
            scheme_screen_payouts=money(scheme_page.context["payouts"]),
            the_benefit=money(BENEFIT),
        )

    # ==================================================================
    # 2. The policy decides, not the person
    # ==================================================================

    def test_the_clerk_who_raised_the_case_cannot_approve_it(self):
        """Segregation of duties, walked rather than asserted on a mixin.

        The policy this church published says a benefit must be approved by
        someone other than the person who raised the case. The clerk holds
        scheme administration; approving a benefit is a money decision and is
        not administration.
        """
        membership = self._grace_enrolled_and_paid_up()
        spouse = membership.dependants.get(
            relationship=SchemeDependant.Relationship.SPOUSE)
        case = self._raise_case(membership, spouse)
        self._case_action(case, "submit")
        self._case_action(case, "assess")

        self.clerk.post(reverse("benevolent_case_decide", args=[case.pk, "approve"]),
                        {"amount": str(BENEFIT)})
        case.refresh_from_db()
        self.assertEqual(
            case.status, BenevolentCase.Status.ASSESSED,
            "the clerk who raised the case approved their own benefit")
        self.assertIsNone(case.approved_amount)

        # nor can the read-only auditor
        reading_room = self.acting_as(self.auditor)
        self.visit(reading_room, "benevolent_case_detail", args=[case.pk])
        reading_room.post(reverse("benevolent_case_decide", args=[case.pk, "approve"]),
                          {"amount": str(BENEFIT)})
        case.refresh_from_db()
        self.assertEqual(case.status, BenevolentCase.Status.ASSESSED)

        # and the fund is untouched by either attempt
        self.assert_fund_balance(self.welfare_fund, OPENING_GIFT + DUES * 6,
                                 as_of=TODAY)
        self.assert_books_balance("after two refused approvals")

    def test_a_member_in_arrears_is_paid_the_benefit_less_exactly_what_they_owe(self):
        """The policy says DEDUCT: pay the family, net off the debt.

        The interesting part is not that a deduction happens — it is that the
        deduction is measured as at the EVENT date while the register shows the
        member's arrears as at today, and the two are different numbers that
        must each be right. A member who stopped paying four months ago owes
        more today than they owed the day of the funeral, and the benefit is
        reduced by the funeral-day figure.
        """
        # Peter enrols beside Grace and pays only his first two months.
        peter = self._enrol(self.peter, spouse_name="Alice Otieno")
        self._admit(peter)
        self._pay_dues(peter, [5, 4])

        spouse = peter.dependants.get(
            relationship=SchemeDependant.Relationship.SPOUSE)
        case = self._raise_case(peter, spouse)
        self._case_action(case, "submit")
        self._case_action(case, "assess")

        # What he owed on the day of the death: four periods had fallen due
        # (the month cover began, and the three after it); he had paid two.
        from benevolent.services.contributions import arrears_for
        owed_at_event = arrears_for(peter, self.policy, as_of=EVENT_DATE)
        self.assertEqual(owed_at_event, DUES * 2,
                         "the arrears at the event date are not what the dues "
                         "schedule says they are")

        self.assert_agree(
            "the benefit for a member in arrears",
            assessed_by_the_engine=money(case.assessed_amount),
            benefit_less_arrears_at_the_event=money(BENEFIT - owed_at_event),
        )
        self.assertIn("Arrears", " ".join(
            case.eligibility_snapshot["entitlement"]["deductions"]),
            "the deduction must be shown on the case, not applied silently")

        # He is still eligible: arrears under a DEDUCT policy reduce a benefit,
        # they do not refuse a bereaved family.
        self.assertTrue(case.eligible)

        # And he stands in ARREARS — a fact about him, not a verdict on his
        # claim — measured as at TODAY, which is a different and larger figure
        # than the one that was deducted, and must not be confused with it.
        from benevolent.services.standing import assess
        peter.refresh_from_db()
        self.assertEqual(assess(peter).standing, Standing.ARREARS)
        owed_today = arrears_for(peter, self.policy, as_of=TODAY)
        self.assertEqual(owed_today, DUES * 4)
        self.assertGreater(
            owed_today, owed_at_event,
            "this test is worthless unless the two dates really do differ")

        # Pay it, and the fund falls by the REDUCED benefit
        self._approve_case(case, case.assessed_amount)
        payout = self._raise_voucher(case, case.approved_amount)
        self._approve_voucher(payout.expense)

        self.assert_fund_balance(
            self.welfare_fund,
            OPENING_GIFT + DUES * 2 - (BENEFIT - owed_at_event), as_of=TODAY)
        self.assert_books_balance("after paying a reduced benefit")
        self.assert_trial_balance_balances()

    # ==================================================================
    # 3. The member's own side of it
    # ==================================================================

    def _invite_and_activate(self, member):
        """The whole invitation, walked: the office invites, the member takes it
        up through the app's ordinary forgot-password flow, and signs in.

        This is failure #122's shape — an invitation that dead-ends on itself —
        so it is deliberately walked end to end rather than short-cut through
        `portal.activate()`. Nothing here is a service call.
        """
        response = self.submit(self.office, "portal_admin_accounts", {
            "action": "invite", "member": member.id,
            "email": "", "phone": member.phone})
        self._no_error_message(response, f"Inviting {member.name} to the portal")
        account = MemberAccount.objects.get(member=member)
        self.assertEqual(account.status, MemberAccount.Status.INVITED)

        # The member asks for a password. The application texts a one-time code;
        # we read it out of the message it actually sent.
        self.sent_messages.clear()
        member_browser = self.client_class()
        member_browser.post(reverse("self_reset_request"),
                            {"username": account.user.username})
        if not self.sent_messages:
            raise WorkflowError(
                f"{member.name} asked to set a password and nothing was sent. "
                f"The invitation dead-ends: the account has no usable password "
                f"and no way to get one.")
        code = re.search(r"\b(\d{6})\b", self.sent_messages[-1][1]).group(1)

        member_browser.post(reverse("self_reset_verify"), {
            "code": code, "new_password": PORTAL_PASSWORD,
            "confirm_password": PORTAL_PASSWORD})
        account.user.refresh_from_db()
        self.assertTrue(
            account.user.check_password(PORTAL_PASSWORD),
            "the member set a password through the app's own reset flow and it "
            "did not take — they cannot get in")

        # They sign in. Signing in is what activates the invitation.
        signed_in = member_browser.login(username=account.user.username,
                                         password=PORTAL_PASSWORD)
        if not signed_in:
            raise WorkflowError(f"{member.name} cannot sign in after setting a "
                                f"password. The invitation dead-ends.")
        account.refresh_from_db()
        self.assertEqual(
            account.status, MemberAccount.Status.ACTIVE,
            "signing in with a password the member set themselves is what "
            "proves the invitation was taken up; the account is still INVITED, "
            "so every portal page will turn them away")
        return account, member_browser

    def test_the_member_signs_in_and_reads_her_own_case_and_her_own_figures(self):
        membership, case = self._a_paid_bereavement()
        account, phone = self._invite_and_activate(self.grace)

        # 1. she lands on her own home page, not the church's dashboard
        home = self.visit(phone, "portal_home").content.decode()
        self.assertIn(membership.number, home,
                      "the member's own enrolment number is not on her own page")
        self.assertIn("Grace Wanjiru".lower(), home.lower())
        self.assertNotIn("Executive overview", home,
                         "a member is being shown office navigation")

        # 2. her contributions are HERS, and they add up to what she paid
        from benevolent.services.contributions import (contributions_total,
                                                       dues_schedule)
        contributions = self.visit(phone, "portal_contributions")
        self.assert_agree(
            "Grace's dues, as she reads them and as the office reads them",
            portal_total=money(contributions.context["total"]),
            office_total=money(contributions_total(membership=membership)),
            six_months_of_dues=money(DUES * 6),
        )

        # 3. her standing page: good standing, nothing owed, and a real dues
        #    schedule behind it
        standing_page = self.visit(phone, "portal_standing")
        row = standing_page.context["rows"][0]
        self.assertEqual(row["standing"].standing, Standing.GOOD)
        self.assertEqual(row["arrears"], Decimal(0))
        self.assert_agree(
            "the dues Grace has cleared, from the schedule and from the receipts",
            schedule_cleared=money(sum(
                (r["paid"] for r in dues_schedule(membership, self.policy)),
                Decimal(0))),
            receipts=money(DUES * 6),
        )
        self.assertTrue(row["schedule"],
                        "a member paying monthly dues has an empty dues schedule")
        self.assertTrue(row["benefits"],
                        "the standing page shows no indication of what she would "
                        "be entitled to")

        # 4. her case, with the figure that was actually approved
        cases_page = self.visit(phone, "portal_cases")
        self.assertEqual([c.pk for c in cases_page.context["cases"]], [case.pk])
        detail = self.visit(phone, "portal_case_detail", args=[case.pk])
        self.assertEqual(detail.context["case"].pk, case.pk)
        self.assertEqual(detail.context["case"].approved_amount, BENEFIT)
        self.assertTrue(
            detail.context["timeline"],
            "the case timeline is empty — the member is shown no progress at all")
        self.assertTrue(
            detail.context["payouts"],
            "the case was paid and the member's own page shows no payment")

        # 5. and her household, which is populated: a spouse and a daughter
        household = self.visit(phone, "portal_household")
        self.assertEqual(household.context["dependants"].count(), 2)

    def test_the_funding_rules_the_treasurer_ticked_are_actually_adopted(self):
        """DEFECT — "What may fund this scheme" is rendered, validated, and then
        thrown away. Nothing a treasurer ticks there is ever saved.

        WHAT HAPPENS. `_write_the_constitution()` above posts the policy form
        with `funding_methods = ["DUES", "DONATION"]`, exactly as a treasurer
        would tick two boxes under "What may fund this scheme". The POST is
        accepted, the policy saves, and the field on the saved policy is `[]`.
        Re-opening the policy screen shows both boxes unticked.

        WHY. `funding_methods` is declared on `PolicyForm` as a
        `MultipleChoiceField` and listed in `PolicyForm.GROUPS`
        (benevolent/forms.py lines 94 and 168), so it renders and it validates —
        `clean_funding_methods` returns the list into `cleaned_data`. But it is
        NOT in `PolicyForm.Meta.fields`, and neither the form nor
        `PolicyFormView.post` ever assigns `cleaned_data["funding_methods"]` to
        the instance. A ModelForm only writes the fields Meta names, so the
        value is validated into a dictionary nobody reads.

        WHY IT MATTERS. The field's own help text calls it "a rule, not a note:
        it stops a member-funded scheme being quietly subsidised out of the
        church budget without the constitution being changed to allow it", and
        `contributions._check_funding_method()` really does enforce it — but it
        treats an empty list as "nothing declared, no restriction", so for every
        scheme configured on this screen the guard is permanently inert. It is
        also one of `SchemePolicy.RULE_FIELDS`, frozen into every case's
        `policy_snapshot`, so each decided case permanently records a funding
        rule the church never adopted.

        Worse than "cannot be set": the setup wizard (`services/wizard.py`) and
        the policy profiles (`services/profiles.py`) both DO write this field.
        A scheme set up through the wizard has the rule; the first time anyone
        edits any other rule on the policy screen, saving wipes it — which is
        the same "silently discarded field" shape `PolicyForm.grouped()`'s own
        docstring says was already found and fixed once in this form.

        WHAT SHOULD HAPPEN. Ticking the boxes and saving should adopt them.

        Not fixed here — this file owns no application code.
        """
        self.policy.refresh_from_db()
        self.assertEqual(
            sorted(self.policy.funding_methods), ["DONATION", "DUES"],
            "the treasurer ticked 'Member dues' and 'Donations and gifts' on "
            "the policy form and the scheme was saved permitting neither")

        # and the screen they would go back to shows their own ticks gone
        form = self.visit(self.office, "benevolent_policy_edit",
                          args=[self.scheme.pk, self.policy.pk]).context["form"]
        self.assertEqual(
            sorted(form.fields["funding_methods"].initial), ["DONATION", "DUES"])

    def test_a_paid_up_member_is_not_told_she_is_in_arrears(self):
        """DEFECT — a member who has paid every month is shown as in arrears on
        her own page, in the same breath as being told she owes nothing.

        WHAT HAPPENS. Grace enrols, is admitted, and pays all six months' dues
        through the contribution screen. Her portal home page then renders:

            <span class="pill pill-green">In arrears</span>
            Owing   KSh 0.00   ·   Nothing outstanding

        A GREEN pill reading "In arrears", directly above a figure saying she
        owes nothing. On "My standing" it is the other way round: a RED "In
        arrears" pill with the engine's own sentence, "Up to date.", printed
        underneath it. The office register shows her as in arrears too.

        WHY. `SchemeMembership.standing` is a cache of `standing.assess()`, and
        `benevolent/services/registry.py` refreshes it on every lifecycle event
        — enrol, admit, suspend, reinstate, withdraw, transfer, exemption
        granted or revoked — and `services/engine.py` refreshes it on every
        member adjustment. The one event that most obviously changes where a
        member stands, PAYING THEIR DUES, is the only one that does not:
        `contributions.record_contribution()` never calls
        `standing.refresh()`. (`_advance_renewal` does, so a RENEWAL fee
        refreshes it and an ordinary due does not.) The cached verdict
        therefore stays at whatever it was when the member was admitted —
        ARREARS, because dues accrue from the cover date and nothing had been
        paid yet — until the nightly `benevolent_automation` command runs.

        The templates then mix the two sources inside one element:
          * templates/benevolent/portal/home.html lines 19-21 take the pill's
            COLOUR from the live assessment (`row.standing.standing`) and its
            TEXT from the cached column (`row.membership.get_standing_display`);
          * templates/benevolent/portal/standing.html line 9 takes both from the
            cached column, and line 31 prints the LIVE reason beside it.

        WHAT SHOULD HAPPEN. Receipting a member's dues should refresh their
        standing, the same way every other event that changes it already does;
        and no single pill should take its colour from one source and its words
        from another. Either would fix the page; the first fixes the register
        too.

        Not fixed here — this file owns no application code. Marked expected so
        the suite stays green and the finding is not lost.
        """
        membership = self._grace_enrolled_and_paid_up()
        account, phone = self._invite_and_activate(self.grace)

        home = self.visit(phone, "portal_home")
        row = home.context["rows"][0]

        # The engine that owns the answer is right, and so is the figure on the
        # page beside the pill. These pass.
        self.assertEqual(row["standing"].standing, Standing.GOOD)
        self.assertEqual(row["arrears"], Decimal(0))

        # The words the member actually reads. This is the defect.
        self.assertNotIn(
            "In arrears", home.content.decode(),
            "Grace has paid every month and her own home page tells her she is "
            "in arrears, beside a figure saying she owes nothing")

        membership.refresh_from_db()
        self.assertEqual(
            membership.standing, Standing.GOOD,
            "paying dues did not refresh the cached standing, so the register "
            "and the portal both show a paid-up member as in arrears")

    def test_every_portal_page_renders_for_a_member_who_actually_has_something(self):
        """Documented failure #125, walked.

        These pages rendered perfectly for a member invited five minutes ago
        with nothing on their record, and 500'd for a real one. The portal is
        excluded from the seeded smoke sweep, so this is the only place a
        POPULATED member is put through every page of it: a case, a dues
        schedule, a dependant, a payout and a receipt.
        """
        membership, case = self._a_paid_bereavement()
        account, phone = self._invite_and_activate(self.grace)

        for name in ["portal_home", "portal_contributions", "portal_statement",
                     "portal_standing", "portal_household", "portal_cases",
                     "portal_requests", "portal_documents",
                     "portal_notifications", "portal_profile"]:
            with self.subTest(page=name):
                self.visit(phone, name)

        with self.subTest(page="portal_case_detail"):
            self.visit(phone, "portal_case_detail", args=[case.pk])

        from benevolent.models import BenevolentContribution
        receipt_for = (BenevolentContribution.objects
                       .filter(membership=membership).order_by("id").first())
        with self.subTest(page="portal_receipt"):
            self.visit(phone, "portal_receipt", args=[receipt_for.pk])

        # the statement is the one that has to agree with the office's figure
        statement = self.visit(phone, "portal_statement")
        self.assert_agree(
            "Grace's statement",
            portal_statement_total=money(statement.context["total"]),
            dues_she_paid=money(DUES * 6),
        )

    def test_a_member_cannot_open_another_members_case_or_receipt(self):
        """Object-level access, walked as a real request rather than asserted on
        a queryset. A filter that is right in the service and wrong in the view
        is still a leak."""
        grace_membership, grace_case = self._a_paid_bereavement()

        # Peter, enrolled beside her, with a case of his own
        peter = self._enrol(self.peter, spouse_name="Alice Otieno")
        self._admit(peter)
        self._pay_dues(peter, range(5, -1, -1))
        peter_spouse = peter.dependants.get(
            relationship=SchemeDependant.Relationship.SPOUSE)
        peter_case = self._raise_case(peter, peter_spouse)
        self._case_action(peter_case, "submit")
        self._case_action(peter_case, "assess")
        self._approve_case(peter_case, peter_case.assessed_amount)

        account, phone = self._invite_and_activate(self.grace)

        # her own case opens
        self.visit(phone, "portal_case_detail", args=[grace_case.pk])

        # his does not — and it must be a refusal, not an empty page
        refused = phone.get(reverse("portal_case_detail", args=[peter_case.pk]))
        self.assertEqual(
            refused.status_code, 403,
            f"Grace opened Peter's case {peter_case.number} and got "
            f"{refused.status_code}")

        # nor his receipts
        from benevolent.models import BenevolentContribution
        his_receipt = (BenevolentContribution.objects
                       .filter(membership=peter).order_by("id").first())
        refused = phone.get(reverse("portal_receipt", args=[his_receipt.pk]))
        self.assertEqual(refused.status_code, 403,
                         "Grace opened Peter's receipt")

        # and her own case list contains only her own case
        listed = self.visit(phone, "portal_cases").context["cases"]
        self.assertEqual([c.pk for c in listed], [grace_case.pk])

    def test_a_portal_member_cannot_walk_into_the_office(self):
        """The confinement rule, walked. A member login that could reach the
        office case screen would see every family's business, and the portal's
        own scoping would never be consulted at all."""
        membership, case = self._a_paid_bereavement()
        account, phone = self._invite_and_activate(self.grace)

        for name, args in [("benevolent_case_detail", [case.pk]),
                           ("benevolent_registry", []),
                           ("benevolent_dashboard", []),
                           ("dashboard", [])]:
            with self.subTest(office_page=name):
                response = phone.get(reverse(name, args=args), follow=True)
                landed = response.redirect_chain[-1][0] if response.redirect_chain else ""
                self.assertTrue(
                    landed.startswith("/portal/"),
                    f"a portal member reached the office page {name} "
                    f"(landed on {landed or 'it directly'})")
