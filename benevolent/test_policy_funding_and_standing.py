"""Two rules the welfare module wrote down and then failed to keep.

Both are the same species of bug — a fact the application computes correctly and
then fails to CARRY to the place that uses it — and both were found by the
end-to-end welfare workflow rather than by any unit test, because each looks
perfectly correct from inside the function it lives in.

1. "What may fund this scheme" was rendered on the policy screen, validated into
   `cleaned_data`, and dropped on the floor. `PolicyForm.GROUPS` (which decides
   what renders) and `PolicyForm.Meta.fields` (which decides what SAVES) were two
   hand-written copies of the same list of chapters, and `funding_methods` had
   been added to the first and not the second. A ModelForm only writes the fields
   Meta names, so a treasurer ticked "Member dues" and "Donations and gifts", the
   policy saved, and the scheme permitted neither — leaving
   `contributions._check_funding_method()` permanently inert, since it reads an
   empty list as "nothing declared, no restriction". The tests here pin the
   behaviour (ticks are adopted, and then enforced) AND the structure (one list
   feeds both), because pinning only the one field would let the next one drift
   the same way.

2. Paying dues did not refresh the member's cached standing. `registry.py`
   refreshes it on all eight lifecycle events and `engine.py` on every
   adjustment; `record_contribution()` — the commonest event of all — did not, so
   a member who had paid every month kept the ARREARS verdict written when she
   was admitted until the nightly job ran. Her own portal page then read "In
   arrears" over "Owing 0.00 — nothing outstanding", because the pill takes its
   words from the cached column and its figures from the live assessment.

The invariant worth stating once: `SchemeMembership.standing` is a CACHE of
`standing.assess()`. Every write that changes the facts must refresh it, or the
two disagree and the church is shown both answers at once.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import TestCase

from core import roles
from departments.models import Department
from members.models import Member

from .forms import PolicyForm
from .models import (BenevolentContribution, BenevolentScheme, SchemeMembership,
                     SchemePolicy, Standing)
from .services import contributions as contrib_svc
from .services import registry as reg_svc
from .services import schemes as scheme_svc
from .services import standing as standing_svc

TODAY = dt.date.today()


class BenevolentFixture(TestCase):
    """A scheme with monthly dues, whose policy is in force and whose doors are
    open — the shape both defects need, and the shape almost every real church
    scheme has."""

    def setUp(self):
        self.user = User.objects.create_user("welfare-clerk", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-policy-standing",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Bereavement Scheme", code="BPS", fund=self.fund,
            created_by=self.user)
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=TODAY - dt.timedelta(days=400),
            # Admission is not required, so a registered member is covered at
            # once and dues start accruing — otherwise standing would read
            # PENDING and say nothing about arrears at all.
            registration_required=False,
            membership_required=True,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            arrears_treatment=SchemePolicy.ArrearsTreatment.DEDUCT,
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("20000"),
            # Nothing declared, which is where every scheme drafted on the
            # policy screen sat while the field was being discarded — and the
            # state in which the funding guard does nothing at all. The tests
            # below declare methods through the form, which is the path at issue.
            funding_methods=[],
            created_by=self.user)
        scheme_svc.publish_policy(self.policy, user=self.user)
        scheme_svc.activate_scheme(self.scheme, user=self.user)

    def _member(self, name="Grace Wanjiru", phone="254700008001", joined_days_ago=200):
        person = Member.objects.create(name=name, phone=phone)
        return reg_svc.register(
            self.scheme, person, user=self.user,
            joined_on=TODAY - dt.timedelta(days=joined_days_ago))


# ===========================================================================
# 1. The funding rule a treasurer ticks is the funding rule the scheme adopts
# ===========================================================================

class PolicyFormAdoptsFundingMethodsTests(BenevolentFixture):

    def _payload(self, **overrides):
        """The whole constitution, as the policy screen posts it back.

        Derived from the policy already on the scheme rather than spelled out as
        a 70-key literal, so a rule added to the form next year is carried here
        automatically instead of turning this into a form that refuses to
        validate for a reason unrelated to what is under test.
        """
        data = {}
        for key, value in PolicyForm(instance=self.policy).initial.items():
            if isinstance(value, bool):
                # an unticked checkbox posts nothing; "" reads back as False
                data[key] = "on" if value else ""
            elif isinstance(value, dt.date):
                data[key] = value.isoformat()
            elif isinstance(value, list):
                data[key] = value
            elif value is None:
                data[key] = ""
            else:
                data[key] = str(value)
        data.update(overrides)
        return data

    def test_the_ticks_the_treasurer_makes_reach_the_saved_policy(self):
        """The defect itself: validated into cleaned_data, absent from the
        instance. The form used to pass this assertion on cleaned_data and fail
        it on the object it built from the same POST."""
        form = PolicyForm(self._payload(funding_methods=["DUES", "DONATION"]),
                          instance=self.policy)
        self.assertTrue(form.is_valid(), form.errors.as_text())
        self.assertEqual(sorted(form.cleaned_data["funding_methods"]),
                         ["DONATION", "DUES"])
        self.assertEqual(
            sorted(form.save(commit=False).funding_methods),
            ["DONATION", "DUES"],
            "the form validated the funding rule and then built an instance "
            "without it — the value went into a dictionary nobody reads")

    def test_the_saved_rule_survives_to_the_database_and_back_to_the_screen(self):
        """A treasurer who saves and re-opens must see their own ticks. Unticked
        boxes on a re-opened screen are how this was noticed at all."""
        form = PolicyForm(self._payload(funding_methods=["DUES", "DONATION"]),
                          instance=self.policy)
        self.assertTrue(form.is_valid(), form.errors.as_text())
        form.save()

        self.policy.refresh_from_db()
        self.assertEqual(sorted(self.policy.funding_methods), ["DONATION", "DUES"])
        reopened = PolicyForm(instance=SchemePolicy.objects.get(pk=self.policy.pk))
        self.assertEqual(sorted(reopened.fields["funding_methods"].initial),
                         ["DONATION", "DUES"])

    def test_clearing_every_box_is_saved_as_cleared(self):
        """The other direction, and the one a "just default it" fix gets wrong: a
        treasurer removing every restriction must not be silently held to the
        rule they just deleted. `funding_methods` has a model default, so a
        checkbox group that posted nothing could plausibly be read as 'omitted'
        rather than 'emptied'."""
        SchemePolicy.objects.filter(pk=self.policy.pk).update(
            funding_methods=["DUES"])
        form = PolicyForm(self._payload(funding_methods=[]),
                          instance=SchemePolicy.objects.get(pk=self.policy.pk))
        self.assertTrue(form.is_valid(), form.errors.as_text())
        form.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.funding_methods, [])

    def test_a_rule_saved_on_the_screen_is_a_rule_the_money_path_enforces(self):
        """Worth more than the field assertion above: it shows the screen and
        `_check_funding_method()` now meet. A dues-only scheme must refuse a
        donation, and the refusal must name what the constitution does allow."""
        form = PolicyForm(self._payload(funding_methods=["DUES"]),
                          instance=self.policy)
        self.assertTrue(form.is_valid(), form.errors.as_text())
        form.save()

        membership = self._member()
        contrib_svc.record_contribution(
            self.scheme, membership=membership, amount=Decimal("200"),
            date=TODAY, user=self.user)

        benefactor = Member.objects.create(name="Josiah Kimani", phone="254700008009")
        with self.assertRaises(ValidationError) as caught:
            contrib_svc.record_contribution(
                self.scheme, member=benefactor, amount=Decimal("5000"),
                date=TODAY, user=self.user)
        refusal = " ".join(caught.exception.messages).lower()
        self.assertIn("periodic dues", refusal)     # what the scheme IS funded by
        self.assertIn("donation", refusal)          # and what it was just offered


class PolicyFormStructureTests(TestCase):
    """The ratchet. The field assertions above would have passed on a fix that
    simply appended one name to a second hand-written list — which is the fix
    that lets the NEXT field drift exactly as this one did.

    Stated against `PolicyForm.GROUPS` (what the template renders) and
    `PolicyForm.Meta.fields` (what the save writes), never against the module
    constant behind them, so the guard holds however those two are wired
    together."""

    def _chapter_fields(self):
        return {f for _group, fs in PolicyForm.GROUPS for f in fs}

    def test_every_chapter_field_is_a_field_the_form_saves(self):
        self.assertEqual(
            self._chapter_fields() - set(PolicyForm.Meta.fields),
            set(),
            "a rule renders on the policy screen that Meta.fields does not "
            "name, so the screen collects it and the save throws it away — the "
            "exact shape of the funding_methods bug")

    def test_the_form_saves_nothing_that_is_not_a_chapter_or_a_preamble(self):
        """The converse: a field that saves but renders nowhere is a rule no
        treasurer can see. `effective_from` and `notes` are deliberately outside
        the chapters — the first is rendered on its own by the template, the
        second is not a rule."""
        self.assertEqual(
            set(PolicyForm.Meta.fields) - self._chapter_fields()
            - {"effective_from", "notes"},
            set())

    def test_every_versioned_rule_can_be_saved_from_the_policy_screen(self):
        """`test_audit` already guards that every RULE_FIELD RENDERS. Rendering
        was never the problem: funding_methods rendered perfectly. This is the
        same guard on the other half of the round trip."""
        missing = set(SchemePolicy.RULE_FIELDS) - set(PolicyForm.Meta.fields)
        self.assertEqual(
            missing, set(),
            f"versioned policy rule(s) the engine enforces and the policy form "
            f"cannot save, so each one is frozen into every case's "
            f"policy_snapshot at a value nobody chose: {sorted(missing)}")


# ===========================================================================
# 2. Paying dues moves where a member stands
# ===========================================================================

class PayingDuesRefreshesStandingTests(BenevolentFixture):

    def setUp(self):
        super().setUp()
        self.membership = self._member()
        self.membership.refresh_from_db()
        # The starting position, and the reason the stale cache was believable:
        # dues accrue from the cover date, so a member is in arrears from the
        # moment they enrol until they pay.
        self.assertEqual(self.membership.standing, Standing.ARREARS)
        self.owed = contrib_svc.arrears_for(self.membership)
        self.assertGreater(self.owed, Decimal(0))

    def _pay(self, amount):
        return contrib_svc.record_contribution(
            self.scheme, membership=self.membership, amount=amount,
            date=TODAY, user=self.user)

    def test_clearing_the_arrears_clears_the_cached_standing(self):
        self._pay(self.owed)
        self.membership.refresh_from_db()
        self.assertEqual(
            self.membership.standing, Standing.GOOD,
            "a member who has just paid everything she owes is still recorded "
            "as in arrears, so the register and her own portal page both say so")

    def test_the_cache_agrees_with_the_engine_that_owns_the_answer(self):
        """The invariant, not the instance. The portal's green pill reading "In
        arrears" was one element taking its colour from `assess()` and its words
        from this column; they must never be able to differ."""
        self._pay(self.owed)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.standing,
                         standing_svc.assess(self.membership).standing)
        self.assertEqual(self.membership.standing_reason,
                         standing_svc.assess(self.membership).reason[:200])
        self.assertEqual(self.membership.standing_as_of, TODAY)

    def test_a_part_payment_leaves_her_in_arrears(self):
        """The refresh must recompute, not assume that money means good standing —
        a member who pays half of what she owes is still behind."""
        self._pay(self.owed / 2)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.standing, Standing.ARREARS)
        self.assertGreater(contrib_svc.arrears_for(self.membership), Decimal(0))

    def test_receipting_money_cannot_overturn_a_treasurer_s_decision(self):
        """`refresh()` writes only the derived axis, and this is why that
        matters here: paying dues must not quietly readmit a member somebody
        suspended. The lifecycle is a human decision and outranks any
        calculation."""
        reg_svc.suspend(self.membership, user=self.user,
                        reason="Under investigation by the welfare committee.")
        self._pay(self.owed)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.status, SchemeMembership.Status.SUSPENDED)
        self.assertEqual(self.membership.standing, Standing.SUSPENDED)

    def test_a_gift_from_someone_who_is_not_a_member_refreshes_nothing(self):
        """Only a member can stand anywhere. A stranger's donation carries no
        membership, and the refresh must not go looking for one."""
        benefactor = Member.objects.create(name="Josiah Kimani", phone="254700008009")
        contribution = contrib_svc.record_contribution(
            self.scheme, member=benefactor, amount=Decimal("5000"),
            date=TODAY, user=self.user)
        self.assertEqual(contribution.kind, BenevolentContribution.Kind.DONATION)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.standing, Standing.ARREARS)

    def test_the_change_of_standing_is_on_the_member_s_record_as_automatic(self):
        """A member has a right to know when the scheme's view of them changed,
        and the entry is now dated the day she paid rather than the night the
        job happened to run.

        It is logged as AUTOMATIC and with no actor on purpose. The clerk
        decided to receipt money; she did not decide that Grace is now in good
        standing — the arithmetic did, and it would have said the same thing
        overnight with nobody present. Who took the money is on the receipt.
        """
        from .models import MembershipEvent
        self._pay(self.owed)
        event = (self.membership.events
                 .filter(kind=MembershipEvent.Kind.STANDING)
                 .order_by("-id").first())
        self.assertIsNotNone(
            event, "her standing changed and nothing was written to her record")
        self.assertEqual(event.to_value, Standing.GOOD)
        self.assertEqual(event.from_value, Standing.ARREARS)
        self.assertTrue(event.automated)
        self.assertIsNone(event.actor_id)
