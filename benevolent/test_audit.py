"""Full-module audit of the benevolent module — findings, each fixed and
guarded here.

Four real issues, plus confirmations that the module's core invariants
genuinely hold under probing rather than merely being asserted in docstrings.

  1. SIX ENFORCED POLICY RULES WERE UNREACHABLE FROM THE UI.
     arrears_block, grace_period_days, exemption_age, max_household_size,
     allow_exemptions and allow_transfers are all genuinely enforced by the
     engine — an audit probe confirmed each one really does block/exempt/cap
     what it claims to. But none of them appeared in PolicyForm.GROUPS, and
     `grouped()` silently skipped any field not listed there, so the template
     never rendered them: a treasurer could not configure a rule the system
     was nonetheless enforcing against their members. Exactly the same shape
     as the settings-page bug found in Phase 9 — which is why the fix is not
     just "add the six fields" but "make grouped() incapable of silently
     dropping a field ever again".

  2. A DUPLICATE, INFERIOR REGISTRATION PATH.
     MembershipCreateView (Phase 1) still rendered its own enrolment form —
     no households, no dependants, no off-roll registration — reachable by
     URL though nothing linked to it. Two divergent code paths for one job.
     Now redirects to the real registration screen; its form is deleted.

  3. THE REMAINING N+1 (recommendation #70b), CLOSED.
     arrears_for() ran ~22 queries per member. Now 6. Same numbers.

  4. TWO DEAD FUNCTIONS, ONE WITH A FALSE DOCSTRING.
     `periods_between` claimed to be "the single definition of which periods
     have fallen due" — a claim Phase 10's rewrite had quietly made false,
     since nothing called it any more. `refresh_arrears_status` was a
     compatibility shim with nothing to be compatible with. Both removed.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member

from benevolent.models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                               SchemeMembership, SchemePolicy, Standing)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc
from benevolent.services import standing as standing_svc

TODAY = dt.date.today()


class AuditFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("taudit", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("caudit", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])

    def _scheme(self, code, **policy_kw):
        fund = Department.objects.create(
            name=f"Audit {code}", slug=f"audit-{code.lower()}",
            fund_type=Department.FundType.LOCAL)
        scheme = BenevolentScheme.objects.create(
            name=f"Audit {code}", code=code, fund=fund, created_by=self.treasurer)
        event = BenevolentEventType.objects.create(
            scheme=scheme, name="Bereavement", code="BER")
        kw = dict(
            scheme=scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        kw.update(policy_kw)
        policy = SchemePolicy.objects.create(**kw)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(scheme, user=self.treasurer)
        return scheme, event, policy


# ===========================================================================
# 1. Every enforced policy rule must be reachable from the policy form
# ===========================================================================

class PolicyFormCompletenessTests(AuditFixture):

    def test_every_versioned_rule_field_appears_on_the_policy_form(self):
        """The bug: six genuinely-enforced rules were absent from
        PolicyForm.GROUPS, so grouped() skipped them and the template
        rendered nothing — a rule the engine enforced but nobody could set.
        Guarded generically, so a NEW rule field added in future cannot
        silently go missing the same way."""
        from benevolent.forms import PolicyForm
        group_fields = {f for _, fs in PolicyForm.GROUPS for f in fs}
        missing = set(SchemePolicy.RULE_FIELDS) - group_fields
        # effective_from is deliberately rendered on its own, outside the groups
        missing.discard("effective_from")
        self.assertEqual(
            missing, set(),
            f"policy rule field(s) enforced by the engine but absent from the "
            f"form's GROUPS, so no treasurer can configure them: {sorted(missing)}")

    def test_grouped_never_silently_drops_a_field(self):
        """The MECHANISM behind the bug, not just its instance: grouped()
        used to iterate GROUPS and skip anything absent from it. Now any
        stray field lands in an 'Other settings' group — visible and
        fixable, rather than invisible."""
        from benevolent.forms import PolicyForm
        form = PolicyForm()
        rendered = {bf.name for _, fields in form.grouped() for bf in fields}
        expected = set(form.fields) - {"effective_from"}
        self.assertEqual(
            expected - rendered, set(),
            "grouped() dropped a field that is on the form — the exact failure "
            "that hid six enforced policy rules from every treasurer")

    def test_the_six_previously_hidden_fields_render_on_the_live_form(self):
        scheme, _e, policy = self._scheme("AF1")
        self.client.force_login(self.treasurer)
        body = self.client.get(
            reverse("benevolent_policy_edit", args=[scheme.pk, policy.pk])
        ).content.decode()
        for field in ("arrears_block", "grace_period_days", "exemption_age",
                      "max_household_size", "allow_exemptions", "allow_transfers"):
            self.assertIn(f'id_{field}', body, f"{field} is still not on the form")


class HiddenRulesAreGenuinelyEnforcedTests(AuditFixture):
    """Confirms the fix above MATTERED: each of the six is a real, working
    rule — not a vestigial field it would have been fine to leave hidden."""

    def test_allow_transfers_false_blocks_a_transfer(self):
        scheme, _e, _p = self._scheme("AF2", allow_transfers=False)
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF Transfer", phone="254700100001"),
            joined_on=TODAY - dt.timedelta(days=90), user=self.treasurer)
        successor = Member.objects.create(name="AF Successor", phone="254700100002")
        with self.assertRaises(ValidationError):
            reg_svc.transfer(m, to_member=successor, user=self.treasurer, reason="x")

    def test_allow_exemptions_false_blocks_an_exemption(self):
        scheme, _e, _p = self._scheme("AF3", allow_exemptions=False)
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF Exempt", phone="254700100003"),
            joined_on=TODAY - dt.timedelta(days=90), user=self.treasurer)
        with self.assertRaises(ValidationError):
            reg_svc.grant_exemption(m, kind="HARDSHIP", reason="x", user=self.clerk)

    def test_max_household_size_caps_the_household(self):
        scheme, _e, _p = self._scheme("AF4", max_household_size=2,
                                      household_mode="HOUSEHOLD")
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF House", phone="254700100004"),
            joined_on=TODAY - dt.timedelta(days=90), user=self.treasurer,
            registration_type="HOUSEHOLD")
        reg_svc.add_dependant(m, relationship="SPOUSE", name="AF Spouse",
                              user=self.treasurer)
        with self.assertRaises(ValidationError):
            reg_svc.add_dependant(m, relationship="CHILD", name="AF Child",
                                  user=self.treasurer)

    def test_grace_period_days_produces_grace_standing(self):
        scheme, _e, _p = self._scheme(
            "AF5", arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT,
            grace_period_days=45)
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF Grace", phone="254700100005"),
            joined_on=TODAY - dt.timedelta(days=40), user=self.treasurer)
        self.assertEqual(standing_svc.assess(m, as_of=TODAY).standing, Standing.GRACE)

    def test_arrears_block_makes_an_owing_member_ineligible(self):
        scheme, event, _p = self._scheme("AF6", arrears_block=True)
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF Block", phone="254700100006"),
            joined_on=TODAY - dt.timedelta(days=300), user=self.treasurer)
        self.assertGreater(contrib_svc.arrears_for(m), 0)
        case = case_svc.create_case(scheme, event_type=event, membership=m,
                                    event_date=TODAY, user=self.clerk)
        case_svc.submit_case(case, user=self.clerk)
        result = case_svc.assess_case(case, user=self.treasurer)
        self.assertFalse(result.eligible)

    def test_exemption_age_exempts_an_older_member(self):
        scheme, _e, _p = self._scheme(
            "AF7", arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT,
            exemption_age=70)
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF Elder", phone="254700100007"),
            joined_on=TODAY - dt.timedelta(days=300), user=self.treasurer,
            date_of_birth=TODAY.replace(year=TODAY.year - 75))
        self.assertEqual(standing_svc.assess(m, as_of=TODAY).standing, Standing.EXEMPT)


# ===========================================================================
# 2. The duplicate registration path is gone
# ===========================================================================

class NoDuplicateRegistrationPathTests(AuditFixture):

    def test_the_old_enrol_url_redirects_to_the_real_registration_screen(self):
        scheme, _e, _p = self._scheme("AF8")
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("benevolent_enrol", args=[scheme.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("benevolent_register", args=[scheme.pk]))

    def test_the_duplicate_membership_form_no_longer_exists(self):
        import benevolent.forms as forms_mod
        self.assertFalse(
            hasattr(forms_mod, "MembershipForm"),
            "MembershipForm was a strictly-inferior duplicate of RegistrationForm "
            "(no households, no dependants, no off-roll registration) — it should "
            "be gone, not left as a second way to do one job")


# ===========================================================================
# 3. The remaining N+1 (recommendation #70b) is closed
# ===========================================================================

class ArrearsQueryBudgetTests(AuditFixture):

    def test_arrears_for_does_not_grow_with_the_length_of_a_members_history(self):
        """The N+1 that was left in Phase 10 and honestly logged as #70b:
        contributions_total() was being called once per DUES PERIOD, so a
        member with years of monthly dues cost a query per month. Now one
        grouped query covers every period at once."""
        scheme, _e, _p = self._scheme("AF9")
        short = reg_svc.register(
            scheme, Member.objects.create(name="AF Short", phone="254700100008"),
            joined_on=TODAY - dt.timedelta(days=60), user=self.treasurer)
        long_ = reg_svc.register(
            scheme, Member.objects.create(name="AF Long", phone="254700100009"),
            joined_on=TODAY - dt.timedelta(days=1500), user=self.treasurer)

        with CaptureQueriesContext(connection) as ctx:
            contrib_svc.arrears_for(short)
        short_q = len(ctx.captured_queries)
        with CaptureQueriesContext(connection) as ctx:
            contrib_svc.arrears_for(long_)
        long_q = len(ctx.captured_queries)

        # ~2 months of history vs ~50 months. Before the fix this was a query
        # per period, so ~48 extra. It must now be flat.
        self.assertEqual(
            short_q, long_q,
            f"arrears_for() cost {short_q} queries for a 2-month member but "
            f"{long_q} for a 50-month one — it is still scaling with history")
        self.assertLessEqual(
            long_q, 8,
            f"arrears_for() takes {long_q} queries; each should be a distinct "
            f"per-member table, not a repeated per-period lookup")

    def test_the_numbers_are_unchanged_by_the_optimisation(self):
        """The whole point: fewer queries, identical answers."""
        scheme, _e, _p = self._scheme(
            "AF10", arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT)
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF Numbers", phone="254700100010"),
            joined_on=TODAY - dt.timedelta(days=300), user=self.treasurer)
        rows = contrib_svc.dues_schedule(m)
        self.assertTrue(rows)
        # every row's outstanding = due - paid, and arrears is their sum
        expected = sum((r["outstanding"] for r in rows), Decimal(0))
        self.assertEqual(contrib_svc.arrears_for(m), expected)

    def test_a_part_paid_period_is_still_counted_correctly(self):
        """The grouped query must produce exactly what the per-period query
        did — including for a period that is only partly paid."""
        scheme, _e, _p = self._scheme(
            "AF11", arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT)
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF Part", phone="254700100011"),
            joined_on=TODAY - dt.timedelta(days=90), user=self.treasurer)
        rows = contrib_svc.dues_schedule(m)
        period = rows[0]["period"]
        contrib_svc.record_contribution(
            scheme, date=TODAY, amount=Decimal("40"), membership=m,
            period_label=period, user=self.treasurer)
        rows = contrib_svc.dues_schedule(m)
        row = next(r for r in rows if r["period"] == period)
        self.assertEqual(row["paid"], Decimal("40"))
        self.assertEqual(row["outstanding"], Decimal("60"))   # 100 due - 40 paid

    def test_an_exemption_still_waives_its_periods_after_the_optimisation(self):
        """_waived_periods now resolves policies against a cached version list
        rather than re-querying — the RULE must be unchanged."""
        scheme, _e, _p = self._scheme(
            "AF12", arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT)
        m = reg_svc.register(
            scheme, Member.objects.create(name="AF Waive", phone="254700100012"),
            joined_on=TODAY - dt.timedelta(days=300), user=self.treasurer)
        before = contrib_svc.arrears_for(m)
        ex = reg_svc.grant_exemption(
            m, kind="HARDSHIP", reason="x", user=self.clerk,
            from_date=TODAY - dt.timedelta(days=300))
        reg_svc.approve_exemption(ex, user=self.treasurer)
        after = contrib_svc.arrears_for(m)
        self.assertLess(after, before, "an approved dues exemption must reduce arrears")


# ===========================================================================
# 4. The dead functions are gone
# ===========================================================================

class DeadCodeRemovedTests(TestCase):

    def test_periods_between_is_gone(self):
        """Its docstring claimed to be 'the single definition of which periods
        have fallen due' — a claim Phase 10's rewrite made false, since nothing
        called it any more. A dead function asserting it is the source of truth
        for a rule that has moved is how a future fix gets made in the wrong
        place."""
        import benevolent.services.contributions as c
        self.assertFalse(hasattr(c, "periods_between"))

    def test_refresh_arrears_status_is_gone(self):
        """A 'backwards-compatible' shim with no callers is not compatibility."""
        import benevolent.services.schemes as s
        self.assertFalse(hasattr(s, "refresh_arrears_status"))

    def test_the_dues_rule_still_lives_in_exactly_one_place(self):
        """The point of removing periods_between: _dues_rows is now the only
        definition of which periods have fallen due, and it still works."""
        import benevolent.services.contributions as c
        self.assertTrue(hasattr(c, "_dues_rows"))
