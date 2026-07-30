"""The constitution wizard can configure a scheme end to end.

A treasurer who sets up a scheme through the wizard should not have to open the
raw policy form afterwards to reach a setting the wizard never asked about —
particularly now that several of those settings actively refuse operations.
Before this, the wizard set 39 of the 71 policy parameters; the other 32 were
left at defaults the treasurer never saw.

`test_every_policy_parameter_is_reachable` is the guard that matters. It walks
four scheme shapes — a levy scheme, a dues scheme, a percentage-benefit scheme
and a fixed-benefit scheme — and requires every parameter to be set by at least
one of them. A parameter added to the policy without a wizard question fails it.

Also here: `household_mode` means what its label says. The wizard's individual
option used to read "the member alone (plus any dependants they register)" while
the field was later made to forbid dependants outright — the label and the rule
contradicting each other, which is how a treasurer ends up with a scheme that
refuses the very registrations the setup screen promised.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase

from core import roles
from departments.models import Department
from members.models import Member

from .models import BenevolentScheme, SchemeMembership, SchemePolicy
from .services import registry as reg_svc
from .services import schemes as scheme_svc
from .services import wizard

#: Bookkeeping on the policy record, not decisions a constitution makes.
NOT_PARAMETERS = {
    "id", "scheme", "effective_from", "effective_to", "published_at",
    "published_by", "created_at", "updated_at", "superseded_by", "notes",
    "version", "created_by", "status",
    # Deprecated duplicate of registration_fee; the wizard asks the question
    # once and writes the field that everything charges against.
    "joining_fee",
}

_COMMON = {
    "purpose": "BENEVOLENT", "household": "HOUSEHOLD", "max_dependants": "8",
    "joining_fee": "300", "arrears": "BLOCK", "renewal": "ANNUAL", "renewal_month": "1",
    "allow_exemptions": "YES", "allow_override": "YES", "allow_transfers": "YES",
    "transfer_membership_on_death": "YES", "require_different_approver": "YES",
    "claim_documents": "DOCUMENTS", "catch_up_restores_eligibility": "YES",
}

#: Four shapes, chosen to exercise every branch of the translation.
SCHEME_SHAPES = {
    "levy": dict(_COMMON, funding="PER_CASE_LEVY", levy_amount="500",
                 max_levies_per_year="6", benefit="POOLED",
                 approval="COMMITTEE", committee_requires_chair="YES",
                 inactivity="FLAG", inactivity_months="12",
                 inactivity_missed_cases="3",
                 inactivity_missed_cases_window="CONSECUTIVE",
                 refunds="PART", refund_percent="50",
                 registration_fee_refundable="NO",
                 bereaved_levy="REDUCED", bereaved_reduction="50"),
    "dues": dict(_COMMON, funding="FIXED_PERIODIC", dues_amount="200",
                 dues_frequency="MONTHLY", benefit="PERCENTAGE",
                 benefit_percent="80", benefit_cap="50000",
                 approval="TWO_STAGE", committee_threshold="20000",
                 inactivity="LAPSE", rejoin_wait="90", refunds="ALL",
                 bereaved_levy="DEDUCT"),
    "fixed_benefit": dict(_COMMON, funding="FIXED_PERIODIC", dues_amount="200",
                          dues_frequency="MONTHLY", benefit="FIXED",
                          benefit_amount="50000", approval="TREASURER",
                          inactivity="NONE", refunds="NONE"),
    "individual": dict(_COMMON, household="INDIVIDUAL",
                       funding="PER_CASE_LEVY", levy_amount="500",
                       benefit="POOLED", approval="TREASURER",
                       inactivity="NONE", refunds="NONE"),
}


def _policy_parameters():
    return {f.name for f in SchemePolicy._meta.get_fields()
            if hasattr(f, "attname") and f.name not in NOT_PARAMETERS}


class WizardConfiguresASchemeEndToEndTests(TestCase):

    def test_every_policy_parameter_is_reachable(self):
        covered = set()
        for answers in SCHEME_SHAPES.values():
            config, _lines, _why = wizard.build_config(answers)
            covered |= set(config)
        missing = sorted(_policy_parameters() - covered)
        self.assertFalse(
            missing,
            "These policy parameters cannot be set from the constitution "
            "wizard, so a treasurer who sets a scheme up through it is left "
            "with defaults they never saw and must open the raw policy form to "
            f"change: {', '.join(missing)}")

    def test_every_value_written_is_a_real_policy_field(self):
        """A typo would be stored on a policy that has no such setting."""
        for name, answers in SCHEME_SHAPES.items():
            config, _lines, _why = wizard.build_config(answers)
            for key in config:
                with self.subTest(shape=name, key=key):
                    self.assertTrue(hasattr(SchemePolicy, key))

    def test_every_setting_carries_its_reasoning(self):
        """A wizard that cannot be checked is a wizard that cannot be trusted."""
        for name, answers in SCHEME_SHAPES.items():
            config, _lines, why = wizard.build_config(answers)
            explained = {d.setting for d in why}
            with self.subTest(shape=name):
                self.assertFalse(
                    set(config) - explained,
                    f"{name}: settings written with no explanation: "
                    f"{sorted(set(config) - explained)}")

    def test_the_settings_that_now_refuse_things_are_asked_about(self):
        """Parameters became enforceable in v3.35.0; silence about them is worse
        than a default, because the refusal arrives later with no explanation."""
        config, _lines, _why = wizard.build_config(SCHEME_SHAPES["levy"])
        for key in ("max_levies_per_year", "refund_percent", "household_mode",
                    "funding_methods", "registration_fee_refundable",
                    "inactivity_missed_cases"):
            with self.subTest(parameter=key):
                self.assertIn(key, config)

    def test_the_answers_actually_reach_the_values(self):
        config, _lines, _why = wizard.build_config(SCHEME_SHAPES["levy"])
        self.assertEqual(config["max_levies_per_year"], 6)
        self.assertEqual(config["refund_percent"], 50)
        self.assertEqual(config["inactivity_missed_cases"], 3)
        self.assertIs(config["registration_fee_refundable"], False)
        self.assertEqual(config["household_mode"], "HOUSEHOLD")

    def test_a_config_the_wizard_produces_is_a_valid_policy(self):
        """The end of the job: it has to save."""
        user = User.objects.create_user("tess-wiz", password="office-pass-1")
        user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        fund = Department.objects.create(
            name="Wizard Fund", slug="wiz-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        scheme = BenevolentScheme.objects.create(
            name="Wizard Scheme", code="WIZ", fund=fund, created_by=user,
            status=BenevolentScheme.Status.ACTIVE)
        config, _lines, _why = wizard.build_config(SCHEME_SHAPES["levy"])
        policy = SchemePolicy(scheme=scheme, effective_from=dt.date.today(),
                              **config)
        policy.full_clean(exclude=["scheme", "published_by", "superseded_by"])
        policy.save()
        self.assertIsNotNone(policy.pk)


class HouseholdModeMeansWhatItSaysTests(TestCase):
    """The label and the rule must agree.

    The individual option used to read "the member alone (plus any dependants
    they register)" while the field was made to forbid dependants outright. A
    treasurer choosing the option that promised dependants got a scheme that
    refused them.
    """

    def setUp(self):
        self.user = User.objects.create_user("tess-hh2", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Household Fund", slug="hh-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Household Scheme", code="HHM", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=dt.date.today() - dt.timedelta(days=400),
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY)
        scheme_svc.publish_policy(self.policy, user=self.user)
        person = Member.objects.create(name="Household Member", phone="254700009300")
        self.membership = reg_svc.register(
            self.scheme, person, joined_on=dt.date.today() - dt.timedelta(days=200))
        if self.membership.status == SchemeMembership.Status.PENDING:
            self.membership = reg_svc.admit(self.membership, user=self.user)

    def _set(self, mode):
        SchemePolicy.objects.filter(pk=self.policy.pk).update(household_mode=mode)
        self.policy.refresh_from_db()

    def test_the_individual_option_does_not_promise_dependants(self):
        question = next(q for q in wizard.QUESTIONS if q.key == "household")
        individual = next(o for o in question.options if o.value == "INDIVIDUAL")
        self.assertNotIn("dependant", individual.label.lower())

    def test_the_household_option_says_it_covers_them(self):
        question = next(q for q in wizard.QUESTIONS if q.key == "household")
        household = next(o for o in question.options if o.value == "HOUSEHOLD")
        self.assertIn("household", household.label.lower())

    def test_the_dependant_limit_is_only_asked_for_household_schemes(self):
        """Asking how many dependants are allowed on a scheme that allows none
        is the same contradiction in a different place."""
        visible = {q.key for q in wizard.visible_questions({"household": "INDIVIDUAL"})}
        self.assertNotIn("max_dependants", visible)
        visible = {q.key for q in wizard.visible_questions({"household": "HOUSEHOLD"})}
        self.assertIn("max_dependants", visible)

    def test_an_individual_scheme_refuses_a_dependant(self):
        self._set(SchemePolicy.HouseholdMode.INDIVIDUAL)
        with self.assertRaises(ValidationError):
            reg_svc.add_dependant(self.membership, relationship="CHILD",
                                  name="A Child", user=self.user)

    def test_the_refusal_explains_the_alternative(self):
        self._set(SchemePolicy.HouseholdMode.INDIVIDUAL)
        try:
            reg_svc.add_dependant(self.membership, relationship="CHILD",
                                  name="A Child", user=self.user)
            self.fail("A dependant was accepted on a member-alone scheme.")
        except ValidationError as exc:
            message = " ".join(exc.messages).lower()
            self.assertIn("household", message)
            self.assertIn("own right", message)

    def test_a_household_scheme_accepts_a_dependant(self):
        self._set(SchemePolicy.HouseholdMode.HOUSEHOLD)
        dependant = reg_svc.add_dependant(
            self.membership, relationship="CHILD", name="A Child", user=self.user)
        self.assertEqual(dependant.membership_id, self.membership.pk)

    def test_the_wizard_and_the_rule_agree(self):
        """Choosing individual in the wizard produces a scheme that refuses
        dependants — which is now what the wizard says it will do."""
        config, _lines, _why = wizard.build_config(SCHEME_SHAPES["individual"])
        self.assertEqual(config["household_mode"], "INDIVIDUAL")
        self._set(config["household_mode"])
        with self.assertRaises(ValidationError):
            reg_svc.add_dependant(self.membership, relationship="CHILD",
                                  name="A Child", user=self.user)


class AcceptingTheDefaultsTests(TestCase):
    """A treasurer can take the defaults for the rest and go to the summary.

    The wizard asks seventy questions. Most have a defensible default, and a
    treasurer setting up their first scheme should be able to get a working,
    honest policy in place and come back to the fine print — the alternative is
    abandoning a long form half way and having no scheme at all.

    Nothing is adopted silently. Every defaulted setting appears on the summary
    with the reasoning that produced it, and each can be changed there or on the
    scheme's policy afterwards.
    """

    def setUp(self):
        self.user = User.objects.create_user("tess-skip", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.client_ = __import__("django.test", fromlist=["Client"]).Client()
        self.client_.force_login(self.user)

    def _answer_essentials(self):
        self.client_.post("/benevolent/wizard/0/", {"purpose": "BENEVOLENT"})
        self.client_.post("/benevolent/wizard/1/",
                          {"funding": "PER_CASE_LEVY", "levy_amount": "500"})

    def test_the_offer_is_not_made_before_the_scheme_has_a_shape(self):
        """What a scheme is for and how it is funded cannot be guessed."""
        body = self.client_.get("/benevolent/wizard/0/").content.decode()
        self.assertNotIn("skip_rest", body)

    def test_the_offer_appears_once_the_essentials_are_answered(self):
        body = self.client_.get(
            f"/benevolent/wizard/{wizard.SKIP_ALLOWED_FROM}/").content.decode()
        self.assertIn("skip_rest", body)

    def test_taking_the_defaults_reaches_the_summary(self):
        self._answer_essentials()
        response = self.client_.post(
            f"/benevolent/wizard/{wizard.SKIP_ALLOWED_FROM}/",
            {"skip_rest": "1"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("summary", response.content.decode().lower())

    def test_what_the_treasurer_already_said_is_kept(self):
        """Filling the gaps must not overwrite the answers given."""
        self._answer_essentials()
        self.client_.post(f"/benevolent/wizard/{wizard.SKIP_ALLOWED_FROM}/",
                          {"skip_rest": "1"})
        answers = self.client_.session.get("benevolent_wizard", {})
        self.assertEqual(answers.get("purpose"), "BENEVOLENT")
        self.assertEqual(answers.get("funding"), "PER_CASE_LEVY")
        self.assertEqual(answers.get("levy_amount"), "500")

    def test_the_result_is_a_complete_policy(self):
        self._answer_essentials()
        self.client_.post(f"/benevolent/wizard/{wizard.SKIP_ALLOWED_FROM}/",
                          {"skip_rest": "1"})
        answers = self.client_.session.get("benevolent_wizard", {})
        config, _lines, _why = wizard.build_config(answers)
        self.assertIn("contribution_mode", config)
        self.assertIn("household_mode", config)
        self.assertIn("levy_amount", config)

    def test_every_defaulted_setting_is_explained(self):
        """The safety net: nothing adopted without being shown."""
        self._answer_essentials()
        self.client_.post(f"/benevolent/wizard/{wizard.SKIP_ALLOWED_FROM}/",
                          {"skip_rest": "1"})
        answers = self.client_.session.get("benevolent_wizard", {})
        config, _lines, why = wizard.build_config(answers)
        explained = {d.setting for d in why}
        self.assertFalse(set(config) - explained)

    def test_a_ruled_out_question_is_not_given_a_value(self):
        """A question an earlier answer excluded has no business acquiring one."""
        filled = wizard.fill_defaults({"purpose": "BENEVOLENT",
                                       "funding": "PER_CASE_LEVY",
                                       "household": "INDIVIDUAL"})
        self.assertNotIn("max_dependants", filled)

    def test_defaults_are_the_safer_reading(self):
        """Limits off, safeguards on — the conservative choice, not the loose one."""
        filled = wizard.fill_defaults({"purpose": "BENEVOLENT",
                                       "funding": "PER_CASE_LEVY"})
        config, _lines, _why = wizard.build_config(filled)
        self.assertTrue(config["require_different_approver"])
        self.assertEqual(config["max_levies_per_year"], 0)
