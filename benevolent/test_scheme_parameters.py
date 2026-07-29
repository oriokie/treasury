"""Every scheme parameter must actually do something.

A treasurer setting up a scheme answers seventy-four questions. The setup wizard
asks them, the scheme profiles pre-fill them, the policy form displays them, and
the versioning machinery records every change to them. Several were never read
by anything.

That is worse than a missing feature. A missing feature is visible. A parameter
that stores an answer and ignores it tells the treasurer their scheme is
constituted one way while it behaves another — and the policy history, which
exists precisely so the church can show what its rules were, records a rule that
was never in force.

The one found with money attached: `refund_percent`. A scheme constituted to
return half of a leaver's contributions would hand back all of it, and the
register would show the constitution being followed.

`test_no_policy_parameter_is_silently_ignored` is the guard that matters most
here. It is a ratchet over the parameters known to be inert, so a new one cannot
join them quietly.
"""
import datetime as dt
import os
import re
import subprocess
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from core import roles
from departments.models import Department
from members.models import Member

from .models import (BenevolentCase, BenevolentScheme, ContributionIntake,
                     SchemeMembership, SchemePolicy)
from .services import contributions as contrib_svc
from .services import engine as engine_svc
from .services import registry as reg_svc
from .services import schemes as scheme_svc


class RefundPercentIsHonouredTests(TestCase):
    """A scheme that refunds half must not hand back all of it."""

    def setUp(self):
        self.user = User.objects.create_user("tess-refund", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-refund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Refund Scheme", code="RFD", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=dt.date.today() - dt.timedelta(days=400),
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            refund_contributions_on_exit=True, refund_percent=Decimal("50"))
        scheme_svc.publish_policy(self.policy, user=self.user)

        person = Member.objects.create(name="Leaving Member", phone="254700009001")
        self.membership = reg_svc.register(
            self.scheme, person, joined_on=dt.date.today() - dt.timedelta(days=200))
        if self.membership.status == SchemeMembership.Status.PENDING:
            self.membership = reg_svc.admit(self.membership, user=self.user)
        contrib_svc.record_contribution(
            self.scheme, membership=self.membership, amount=Decimal("1000"),
            date=dt.date.today(), user=self.user)

    def test_a_refund_within_the_policy_is_allowed(self):
        expense = engine_svc.refund(
            self.membership, amount=Decimal("500"), reason="Left the scheme",
            date=dt.date.today(), user=self.user)
        self.assertEqual(expense.amount, Decimal("500"))

    def test_a_refund_beyond_the_policy_is_refused(self):
        with self.assertRaises(ValidationError):
            engine_svc.refund(
                self.membership, amount=Decimal("1000"), reason="Left the scheme",
                date=dt.date.today(), user=self.user)

    def test_the_refusal_says_what_the_scheme_allows(self):
        """A treasurer needs the number, not just a refusal."""
        try:
            engine_svc.refund(
                self.membership, amount=Decimal("1000"), reason="Left",
                date=dt.date.today(), user=self.user)
            self.fail("A refund beyond the policy was accepted.")
        except ValidationError as exc:
            message = " ".join(exc.messages)
            self.assertIn("50", message)
            self.assertIn("500", message)

    def test_an_unset_percent_places_no_ceiling(self):
        """0 is the default and means unspecified, as every other limit here does.

        Reading it as a hard zero would refuse every refund on every scheme that
        never set it — which is all of them.
        """
        SchemePolicy.objects.filter(pk=self.policy.pk).update(
            refund_percent=Decimal("0"))
        expense = engine_svc.refund(
            self.membership, amount=Decimal("1000"), reason="Left",
            date=dt.date.today(), user=self.user)
        self.assertEqual(expense.amount, Decimal("1000"))

    def test_a_scheme_refunding_in_full_is_unaffected(self):
        SchemePolicy.objects.filter(pk=self.policy.pk).update(
            refund_percent=Decimal("100"))
        expense = engine_svc.refund(
            self.membership, amount=Decimal("1000"), reason="Left",
            date=dt.date.today(), user=self.user)
        self.assertEqual(expense.amount, Decimal("1000"))

    def test_a_refund_larger_than_the_member_gave_is_still_refused(self):
        """The older rule, still in force."""
        SchemePolicy.objects.filter(pk=self.policy.pk).update(
            refund_percent=Decimal("100"))
        with self.assertRaises(ValidationError):
            engine_svc.refund(
                self.membership, amount=Decimal("5000"), reason="Left",
                date=dt.date.today(), user=self.user)


class IntakeQueueSeparatesSchemesTests(TestCase):
    """One queue, several schemes — resolving is scheme-by-scheme work."""

    def setUp(self):
        self.user = User.objects.create_user("tess-intake", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-intake",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.a = BenevolentScheme.objects.create(
            name="Bereavement Scheme", code="BRV", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        self.client = Client()
        self.client.force_login(self.user)

    def test_one_scheme_is_offered_no_filter(self):
        """A choice with one option is clutter."""
        body = self.client.get("/benevolent/intake/").content.decode()
        self.assertNotIn('name="scheme"', body)

    def test_a_second_scheme_brings_the_filter(self):
        BenevolentScheme.objects.create(
            name="Medical Scheme", code="MDL", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        body = self.client.get("/benevolent/intake/").content.decode()
        self.assertIn('name="scheme"', body)
        self.assertIn("Medical Scheme", body)

    def test_filtering_by_scheme_is_accepted(self):
        BenevolentScheme.objects.create(
            name="Medical Scheme", code="MDL", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        response = self.client.get("/benevolent/intake/", {"scheme": "MDL"})
        self.assertEqual(response.status_code, 200)

    def test_an_unknown_scheme_code_does_not_error(self):
        response = self.client.get("/benevolent/intake/", {"scheme": "NOPE"})
        self.assertEqual(response.status_code, 200)


class NoPolicyParameterIsSilentlyIgnoredTests(TestCase):
    """A parameter on the policy form must be consulted somewhere.

    The audit is deliberately crude — it looks for the parameter being read off
    a policy-shaped object anywhere in the application — because the failure it
    guards against is absolute: not "read in the wrong place" but "never read at
    all". Anything subtler than that belongs in a test of the parameter itself.

    `KNOWN_INERT` records the parameters already in this state when the guard was
    written. The list may shrink and must never grow. Each is a question a
    treasurer is asked during setup whose answer changes nothing:

      * ``registration_fee_refundable`` — stored, never consulted when a
        registration fee is returned.
      * ``joining_fee`` — the wizard asks for a one-off joining fee and nothing
        ever charges it.
      * ``max_levies_per_year`` — a cap on how often members can be called on,
        set by two of the built-in scheme profiles and enforced nowhere, so a
        scheme promising at most six levies a year will raise twenty.
      * ``household_mode`` and ``funding_methods`` — set by the profiles and the
        wizard; the behaviour they describe is driven by `registration_type` and
        `contribution_mode` instead, so these two are duplicates that can
        disagree with the fields actually in force.
    """

    #: Parameters known to be inert. Shrink only — see the class docstring.
    #: Emptied in v3.35.0: every one of them is now consulted.
    KNOWN_INERT = set()

    #: Not policy decisions: bookkeeping on the policy record itself.
    NOT_PARAMETERS = {
        "id", "scheme", "effective_from", "effective_to", "published_at",
        "published_by", "created_at", "updated_at", "superseded_by", "notes",
        "version", "created_by",
    }

    def _read_somewhere(self, name):
        """Is this parameter read off a policy-shaped object anywhere?"""
        pattern = (rf'(policy|pol|p|cfg|effective|current_policy|self)\.{name}\b'
                   rf'|getattr\([^,]+,\s*["\']{name}["\']')
        roots = [os.path.join(settings.BASE_DIR, d)
                 for d in ("benevolent", "core", "reports")]
        found = subprocess.run(
            ["grep", "-rnE", "--include=*.py", pattern] + roots,
            capture_output=True, text=True).stdout.splitlines()
        return [line for line in found
                if "/migrations/" not in line and "/test" not in line
                and "/forms.py" not in line]

    def test_no_policy_parameter_is_silently_ignored(self):
        inert = []
        for field in SchemePolicy._meta.get_fields():
            name = getattr(field, "name", "")
            if not hasattr(field, "attname") or name in self.NOT_PARAMETERS:
                continue
            if name in self.KNOWN_INERT:
                continue
            if not self._read_somewhere(name):
                inert.append(name)
        self.assertFalse(
            inert,
            "These scheme parameters are stored, shown on the setup form and "
            "versioned, but nothing ever reads them — so a treasurer is "
            "answering questions that change nothing, and the policy history "
            f"records rules that were never in force: {', '.join(sorted(inert))}")

    def test_the_known_list_has_not_grown_stale(self):
        """A parameter that has since been wired up must be struck off."""
        fixed = sorted(name for name in self.KNOWN_INERT
                       if self._read_somewhere(name))
        self.assertFalse(
            fixed,
            f"These are recorded as inert but are now read: {', '.join(fixed)}. "
            "Remove them from KNOWN_INERT so the list keeps meaning something.")

    def test_the_parameters_this_review_wired_up_stay_wired_up(self):
        """Pinned by name so a refactor cannot quietly unhook them again."""
        for name in ("refund_percent", "registration_fee_refundable",
                     "joining_fee", "max_levies_per_year", "household_mode",
                     "funding_methods"):
            with self.subTest(parameter=name):
                self.assertTrue(
                    self._read_somewhere(name),
                    f"{name} is no longer read anywhere — it has gone back to "
                    "being a question the treasurer answers for nothing.")


class EachParameterBehavesTests(TestCase):
    """The five wired up in v3.35.0, each checked by what it actually does.

    Being *read* is necessary and not sufficient — the audit above only proves a
    parameter is mentioned. These prove it changes an outcome.
    """

    def setUp(self):
        self.user = User.objects.create_user("tess-params", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-params",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Parameter Scheme", code="PRM", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        self.event_type = self.scheme.event_types.create(
            name="Bereavement", code="BRV", covers_dependants=True)
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=dt.date.today() - dt.timedelta(days=800),
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            household_mode=SchemePolicy.HouseholdMode.HOUSEHOLD)
        scheme_svc.publish_policy(self.policy, user=self.user)
        person = Member.objects.create(name="Parameter Member", phone="254700009100")
        self.membership = reg_svc.register(
            self.scheme, person, joined_on=dt.date.today() - dt.timedelta(days=700))
        if self.membership.status == SchemeMembership.Status.PENDING:
            self.membership = reg_svc.admit(self.membership, user=self.user)
        self.membership.refresh_from_db()

    def _set(self, **kwargs):
        SchemePolicy.objects.filter(pk=self.policy.pk).update(**kwargs)
        self.policy.refresh_from_db()

    # -- household_mode -------------------------------------------------------

    def test_an_individual_scheme_refuses_a_household_enrolment(self):
        self._set(household_mode=SchemePolicy.HouseholdMode.INDIVIDUAL)
        person = Member.objects.create(name="Household Hopeful", phone="254700009201")
        with self.assertRaises(ValidationError):
            reg_svc.register(self.scheme, person, joined_on=dt.date.today(),
                             registration_type="HOUSEHOLD")

    def test_a_household_scheme_accepts_one(self):
        self._set(household_mode=SchemePolicy.HouseholdMode.HOUSEHOLD)
        person = Member.objects.create(name="Household Member", phone="254700009202")
        m = reg_svc.register(self.scheme, person, joined_on=dt.date.today(),
                             registration_type="HOUSEHOLD")
        self.assertEqual(m.registration_type, "HOUSEHOLD")

    def test_individual_enrolment_is_unaffected(self):
        """The default, and what nearly every existing scheme is sitting on."""
        self._set(household_mode=SchemePolicy.HouseholdMode.INDIVIDUAL)
        person = Member.objects.create(name="Ordinary Member", phone="254700009203")
        m = reg_svc.register(self.scheme, person, joined_on=dt.date.today())
        self.assertIsNotNone(m.pk)

    def test_an_individual_scheme_refuses_a_dependant(self):
        """Enforced only because migration 0033 first made the field true.

        Refusing dependants on the old INDIVIDUAL default would have broken
        every scheme that had been registering them for years. The migration
        corrected those schemes from their own register first, so a scheme still
        marked individual is one that has genuinely never covered a household.
        """
        self._set(household_mode=SchemePolicy.HouseholdMode.INDIVIDUAL)
        with self.assertRaises(ValidationError):
            reg_svc.add_dependant(self.membership, relationship="CHILD",
                                  name="A Child", user=self.user)

    def test_the_refusal_says_which_setting_to_change(self):
        self._set(household_mode=SchemePolicy.HouseholdMode.INDIVIDUAL)
        try:
            reg_svc.add_dependant(self.membership, relationship="CHILD",
                                  name="A Child", user=self.user)
            self.fail("A dependant was accepted on an individual-only scheme.")
        except ValidationError as exc:
            self.assertIn("household", " ".join(exc.messages).lower())

    def test_a_household_scheme_accepts_a_dependant(self):
        self._set(household_mode=SchemePolicy.HouseholdMode.HOUSEHOLD)
        dependant = reg_svc.add_dependant(
            self.membership, relationship="CHILD", name="A Child", user=self.user)
        self.assertEqual(dependant.membership_id, self.membership.pk)

    def test_new_schemes_cover_households_by_default(self):
        """The default that matches how church schemes actually run.

        It was INDIVIDUAL, which is why every scheme drifted into contradicting
        its own policy the first time somebody registered a spouse.
        """
        fresh = SchemePolicy(scheme=self.scheme,
                             effective_from=dt.date.today())
        self.assertEqual(fresh.household_mode,
                         SchemePolicy.HouseholdMode.HOUSEHOLD)

    # -- funding_methods ------------------------------------------------------

    def test_a_kind_the_scheme_is_not_funded_by_is_refused(self):
        self._set(funding_methods=["DUES", "DONATION"])
        with self.assertRaises(ValidationError):
            contrib_svc.record_contribution(
                self.scheme, membership=self.membership, amount=Decimal("100"),
                date=dt.date.today(), kind="LEVY", user=self.user)

    def test_a_permitted_kind_is_accepted(self):
        self._set(funding_methods=["DUES", "DONATION"])
        contrib_svc.record_contribution(
            self.scheme, membership=self.membership, amount=Decimal("100"),
            date=dt.date.today(), kind="DUES", user=self.user)

    def test_declaring_nothing_forbids_nothing(self):
        """An empty list means undeclared, not "no money at all"."""
        self._set(funding_methods=[])
        contrib_svc.record_contribution(
            self.scheme, membership=self.membership, amount=Decimal("100"),
            date=dt.date.today(), kind="LEVY", user=self.user)

    # -- max_levies_per_year --------------------------------------------------

    def _cases(self, n):
        base = dt.date.today()
        for i in range(n):
            BenevolentCase.objects.create(
                scheme=self.scheme, event_type=self.event_type,
                event_date=base - dt.timedelta(days=30 * (i + 1)),
                status=BenevolentCase.Status.PAID)
        return BenevolentCase.objects.create(
            scheme=self.scheme, event_type=self.event_type, event_date=base,
            status=BenevolentCase.Status.APPROVED)

    def test_a_member_at_the_cap_is_not_levied_again(self):
        case = self._cases(3)
        self._set(max_levies_per_year=2)
        roster = contrib_svc.raise_case_levy(case)
        self.assertNotIn(self.membership.pk,
                         {r["membership"].pk for r in roster["rows"]})
        self.assertIn(self.membership.pk, {m.pk for m in roster["over_cap"]})

    def test_a_member_below_the_cap_is_levied(self):
        case = self._cases(3)
        self._set(max_levies_per_year=10)
        roster = contrib_svc.raise_case_levy(case)
        self.assertIn(self.membership.pk,
                      {r["membership"].pk for r in roster["rows"]})

    def test_a_cap_of_zero_means_no_limit(self):
        """The default, and how most schemes run."""
        case = self._cases(3)
        self._set(max_levies_per_year=0)
        roster = contrib_svc.raise_case_levy(case)
        self.assertIn(self.membership.pk,
                      {r["membership"].pk for r in roster["rows"]})

    def test_a_members_own_case_does_not_use_up_their_allowance(self):
        """They were never asked for that one."""
        base = dt.date.today()
        for i in range(3):
            BenevolentCase.objects.create(
                scheme=self.scheme, event_type=self.event_type,
                membership=self.membership,
                event_date=base - dt.timedelta(days=30 * (i + 1)),
                status=BenevolentCase.Status.PAID)
        case = BenevolentCase.objects.create(
            scheme=self.scheme, event_type=self.event_type, event_date=base,
            status=BenevolentCase.Status.APPROVED)
        self._set(max_levies_per_year=2)
        roster = contrib_svc.raise_case_levy(case)
        self.assertIn(self.membership.pk,
                      {r["membership"].pk for r in roster["rows"]})

    # -- joining_fee ----------------------------------------------------------

    def test_a_policy_carrying_only_the_old_joining_fee_still_charges_it(self):
        self._set(registration_fee=Decimal("0"), joining_fee=Decimal("300"))
        self.assertEqual(self.policy.fee_to_join, Decimal("300"))

    def test_the_registration_fee_wins_where_both_are_set(self):
        """One question, two fields; the one everything charges against leads."""
        self._set(registration_fee=Decimal("500"), joining_fee=Decimal("300"))
        self.assertEqual(self.policy.fee_to_join, Decimal("500"))

    def test_no_fee_means_no_fee(self):
        self._set(registration_fee=Decimal("0"), joining_fee=Decimal("0"))
        self.assertEqual(self.policy.fee_to_join, Decimal("0"))

    # -- registration_fee_refundable ------------------------------------------

    def test_a_non_refundable_registration_fee_is_not_returned(self):
        self._set(refund_contributions_on_exit=True, refund_percent=Decimal("100"),
                  registration_fee_refundable=False)
        contrib_svc.record_contribution(
            self.scheme, membership=self.membership, amount=Decimal("300"),
            date=dt.date.today(), kind="REGISTRATION", user=self.user)
        contrib_svc.record_contribution(
            self.scheme, membership=self.membership, amount=Decimal("700"),
            date=dt.date.today(), kind="LEVY", user=self.user)
        with self.assertRaises(ValidationError):
            engine_svc.refund(self.membership, amount=Decimal("1000"),
                              reason="Left", date=dt.date.today(), user=self.user)

    def test_a_refundable_registration_fee_is_returned(self):
        self._set(refund_contributions_on_exit=True, refund_percent=Decimal("100"),
                  registration_fee_refundable=True)
        contrib_svc.record_contribution(
            self.scheme, membership=self.membership, amount=Decimal("300"),
            date=dt.date.today(), kind="REGISTRATION", user=self.user)
        contrib_svc.record_contribution(
            self.scheme, membership=self.membership, amount=Decimal("700"),
            date=dt.date.today(), kind="LEVY", user=self.user)
        expense = engine_svc.refund(
            self.membership, amount=Decimal("1000"), reason="Left",
            date=dt.date.today(), user=self.user)
        self.assertEqual(expense.amount, Decimal("1000"))
