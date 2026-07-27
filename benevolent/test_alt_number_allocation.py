"""A member who gives from a second line is still the member.

People give from more than one number — an M-Pesa line, a work handset, a phone
the household shares. The scheme has always had somewhere to record those
(`MemberPhone`), the member matcher has always used them, and the benevolent
allocator has always had a signal for them.

It had never once fired. The allocator asked for `phones__phone`; the field is
`number`, so every lookup raised a `FieldError` — and it was wrapped in an
`except Exception` written to tolerate the table not existing, which instead
swallowed a plain misspelling for the whole life of the feature. Nothing failed,
nothing was logged, and every payment from a member's second line went to the
review queue to be resolved by hand.

Two further things had to change before it was of any use:

* **The weight.** A second number scored 45 against a gate of 85, so even with
  the member's own name on the narration (45 + 30 = 75) it could not be acted
  on. "Primary" only decides which number a receipt is addressed to; both were
  put on the record by a treasurer, and the member matcher has always treated
  them as equally conclusive.
* **Somewhere to put one.** A second number could previously only arrive as a
  side effect of merging two duplicate records, so a member who had never been
  duplicated had no way to have their other line recognised at all.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from departments.models import Department
from members.models import Member, MemberPhone

from .models import (BenevolentScheme, ContributionRule, SchemeMembership,
                     SchemePolicy)
from .services import allocation as alloc
from .services import registry as reg_svc
from .services import schemes as scheme_svc


class AlternativeNumberIsRecognisedTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("tess-alt", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-alt",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Alt Scheme", code="ALT", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=dt.date.today() - dt.timedelta(days=400),
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY)
        scheme_svc.publish_policy(policy, user=self.user)
        ContributionRule.objects.create(pattern="alt", scheme=self.scheme)

        self.person = Member.objects.create(name="Eliud Kemboi",
                                            phone="254722000111")
        self.membership = reg_svc.register(
            self.scheme, self.person,
            joined_on=dt.date.today() - dt.timedelta(days=200))
        if self.membership.status == SchemeMembership.Status.PENDING:
            self.membership = reg_svc.admit(self.membership, user=self.user)
        self.alt = MemberPhone.objects.create(
            member=self.person, number="254799123456", label="M-Pesa")

    def _allocate(self, phone, name=None):
        return alloc.allocate(reference="alt dues", phone=phone,
                              name=name if name is not None else self.person.name,
                              amount=Decimal("200"), date=dt.date.today())

    def test_a_payment_from_the_second_line_finds_the_member(self):
        result = self._allocate(self.alt.number)
        self.assertTrue(result.candidates, "The second number matched nothing.")
        self.assertEqual(result.candidates[0].membership_id, self.membership.pk)

    def test_the_signal_says_it_was_another_number_of_theirs(self):
        result = self._allocate(self.alt.number)
        codes = {s.code for s in result.candidates[0].signals}
        self.assertIn("member_alt_phone", codes)

    def test_it_is_confident_enough_to_act_on(self):
        """45 could never clear the 85 gate, even with the name."""
        result = self._allocate(self.alt.number)
        self.assertGreaterEqual(
            result.identity_confidence, 85,
            "A member paying from their own second line under their own name "
            "still cannot be allocated automatically.")

    def test_the_primary_number_still_works(self):
        result = self._allocate(self.person.phone)
        self.assertEqual(result.candidates[0].membership_id, self.membership.pk)
        codes = {s.code for s in result.candidates[0].signals}
        self.assertIn("member_phone", codes)

    def test_the_primary_is_not_double_counted(self):
        """A number that is both primary and listed must score once."""
        MemberPhone.objects.create(member=self.person, number=self.person.phone,
                                   is_primary=True)
        result = self._allocate(self.person.phone)
        codes = [s.code for s in result.candidates[0].signals]
        self.assertNotIn("member_alt_phone", codes)

    def test_the_number_matches_however_the_bank_writes_it(self):
        """Stored "254799...", sent "0799..." — one telephone."""
        for form in ("254799123456", "0799123456", "+254799123456"):
            with self.subTest(form=form):
                result = self._allocate(form)
                self.assertTrue(result.candidates, f"{form} matched nothing")
                self.assertEqual(result.candidates[0].membership_id,
                                 self.membership.pk)

    def test_an_unknown_number_still_matches_nobody(self):
        result = self._allocate("254700999888", name="")
        ids = [c.membership_id for c in result.candidates]
        self.assertNotIn(self.membership.pk, ids)

    def test_the_lookup_is_not_silently_swallowed(self):
        """The fault was a FieldError hidden by a blanket except.

        Asserted directly against the model so a future rename is caught here
        rather than by nobody.
        """
        self.assertTrue(
            Member.objects.filter(phones__number=self.alt.number).exists(),
            "MemberPhone's number field has been renamed; the allocator's "
            "lookup must be renamed with it.")


class ManagingAlternativeNumbersTests(TestCase):
    """A treasurer can record a second number without merging two records."""

    def setUp(self):
        self.user = User.objects.create_user("tess-altui", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.member = Member.objects.create(name="Ruth Momanyi", phone="254712000001")
        self.client = Client()
        self.client.force_login(self.user)

    def _add(self, number, label=""):
        return self.client.post(
            reverse("member_phone_add", args=[self.member.pk]),
            {"number": number, "label": label}, follow=True)

    def test_a_second_number_can_be_added(self):
        self._add("0799000002", "M-Pesa")
        self.assertEqual(self.member.phones.count(), 1)

    def test_it_is_stored_in_one_canonical_form(self):
        """Typed "0799...", stored "254799..." — so it matches what banks send."""
        self._add("0799000002")
        self.assertEqual(self.member.phones.first().number, "254799000002")

    def test_the_label_is_kept(self):
        self._add("0799000002", "M-Pesa")
        self.assertEqual(self.member.phones.first().label, "M-Pesa")

    def test_something_that_is_not_a_number_is_refused(self):
        self._add("not a phone")
        self.assertEqual(self.member.phones.count(), 0)

    def test_the_same_number_twice_is_not_duplicated(self):
        self._add("0799000002")
        self._add("0799000002")
        self.assertEqual(self.member.phones.count(), 1)

    def test_the_members_own_primary_number_is_not_added_again(self):
        self._add("0712000001")
        self.assertEqual(self.member.phones.count(), 0)

    def test_a_number_held_for_someone_else_is_refused(self):
        """Two members on one number makes every payment from it ambiguous.

        The honest fix is a merge, not a second copy, so this is refused with
        that said plainly rather than quietly accepted.
        """
        other = Member.objects.create(name="Jane Nyamongo", phone="254733000003")
        self._add("0733000003")
        self.assertEqual(self.member.phones.count(), 0)
        self.assertTrue(Member.objects.filter(pk=other.pk).exists())

    def test_a_number_can_be_removed(self):
        self._add("0799000002")
        phone = self.member.phones.first()
        self.client.post(
            reverse("member_phone_remove", args=[self.member.pk, phone.pk]),
            follow=True)
        self.assertEqual(self.member.phones.count(), 0)

    def test_the_primary_cannot_be_removed_this_way(self):
        """Changing where receipts go is a different decision."""
        primary = MemberPhone.objects.create(
            member=self.member, number=self.member.phone, is_primary=True)
        self.client.post(
            reverse("member_phone_remove", args=[self.member.pk, primary.pk]),
            follow=True)
        self.assertTrue(self.member.phones.filter(pk=primary.pk).exists())

    def test_another_members_number_cannot_be_removed(self):
        other = Member.objects.create(name="Jane Nyamongo", phone="254733000003")
        theirs = MemberPhone.objects.create(member=other, number="254733000009")
        response = self.client.post(
            reverse("member_phone_remove", args=[self.member.pk, theirs.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(MemberPhone.objects.filter(pk=theirs.pk).exists())

    def test_the_page_offers_the_control(self):
        body = self.client.get(
            reverse("member_detail", args=[self.member.pk])).content.decode()
        self.assertIn(reverse("member_phone_add", args=[self.member.pk]), body)

    def test_the_page_says_when_there_are_none(self):
        body = self.client.get(
            reverse("member_detail", args=[self.member.pk])).content.decode()
        self.assertIn("None recorded", body)
