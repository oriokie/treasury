"""Tests for member-merge phone number handling (v2.43):

The underlying preservation (members.services.matching.merge_members ->
MemberPhone) already existed and already worked correctly at the data level
— verified here, not assumed. Two real gaps existed around it and are fixed
here:

1. match_or_create_member (used by every future bank/envelope import) only
   checked a member's PRIMARY phone — a payment from the absorbed member's
   own number, preserved specifically so it would still be recognised,
   would silently fail to match and could create a duplicate member,
   defeating much of the point of preserving it.
2. The preserved secondary numbers were never shown anywhere in the UI —
   correct data nobody could see. Now shown on the member detail page.
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from members.models import Member, MemberPhone
from members.services.matching import match_or_create_member, merge_members


def _treasurer(username="mp_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class MergePhonePreservationTests(TestCase):
    """The underlying data-level behaviour — confirming it actually works,
    not just that the docstring claims it does."""

    def test_both_numbers_preserved_after_merge(self):
        m1 = Member.objects.create(name="Preserve Test", phone="0712345678")
        m2 = Member.objects.create(name="Preserve Test", phone="0798765432")
        kept = merge_members(m1, m2)
        numbers = set(kept.phones.values_list("number", flat=True))
        self.assertIn("254712345678", numbers)
        self.assertIn("254798765432", numbers)

    def test_keeps_existing_primary_not_absorbed_ones(self):
        m1 = Member.objects.create(name="Primary Test", phone="0712345678")
        m2 = Member.objects.create(name="Primary Test", phone="0798765432")
        kept = merge_members(m1, m2)
        kept.refresh_from_db()
        self.assertEqual(kept.phone, "254712345678")
        primary_rows = kept.phones.filter(is_primary=True)
        self.assertEqual(primary_rows.count(), 1)
        self.assertEqual(primary_rows.first().number, "254712345678")

    def test_absorbed_member_deleted(self):
        m1 = Member.objects.create(name="Del Test", phone="0712345678")
        m2 = Member.objects.create(name="Del Test", phone="0798765432")
        merge_members(m1, m2)
        self.assertFalse(Member.objects.filter(pk=m2.pk).exists())

    def test_no_duplicate_numbers_when_both_share_one(self):
        # both records happen to already know the SAME number — must not
        # create two MemberPhone rows for the identical number
        m1 = Member.objects.create(name="Same Num", phone="0712345678")
        m2 = Member.objects.create(name="Same Num", phone="0712345678")
        kept = merge_members(m1, m2)
        self.assertEqual(kept.phones.filter(number="254712345678").count(), 1)

    def test_merge_preserves_third_number_already_on_keep(self):
        m1 = Member.objects.create(name="Three Nums", phone="0711111111")
        MemberPhone.objects.create(member=m1, number="254722222222")
        m2 = Member.objects.create(name="Three Nums", phone="0733333333")
        kept = merge_members(m1, m2)
        numbers = set(kept.phones.values_list("number", flat=True))
        self.assertEqual(numbers, {"254711111111", "254722222222", "254733333333"})


class MatchingUsesSecondaryNumbersTests(TestCase):
    """The correctness gap: future payments must find a member via ANY of
    their preserved numbers, not just the primary."""

    def test_future_payment_from_absorbed_number_matches_existing_member(self):
        m1 = Member.objects.create(name="Jane Doe", phone="0712345678")
        m2 = Member.objects.create(name="Jane Doe", phone="0798765432")
        kept = merge_members(m1, m2)

        found, outcome = match_or_create_member("Jane Doe", "0798765432")
        self.assertEqual(outcome, "matched_phone")
        self.assertEqual(found.pk, kept.pk)

    def test_future_payment_from_primary_still_matches(self):
        m1 = Member.objects.create(name="Jane Doe", phone="0712345678")
        m2 = Member.objects.create(name="Jane Doe", phone="0798765432")
        kept = merge_members(m1, m2)

        found, outcome = match_or_create_member("Jane Doe", "0712345678")
        self.assertEqual(outcome, "matched_phone")
        self.assertEqual(found.pk, kept.pk)

    def test_no_duplicate_member_created_from_secondary_number(self):
        m1 = Member.objects.create(name="Jane Doe", phone="0712345678")
        m2 = Member.objects.create(name="Jane Doe", phone="0798765432")
        merge_members(m1, m2)
        match_or_create_member("Jane Doe", "0798765432")
        # Member.save() stores names uppercase (a pre-existing, deliberate
        # design choice for consistent matching across imports) — query
        # accordingly rather than assuming the as-typed case round-trips
        self.assertEqual(Member.objects.filter(name="JANE DOE").count(), 1)

    def test_unrelated_number_still_creates_new_member(self):
        Member.objects.create(name="Jane Doe", phone="0712345678")
        found, outcome = match_or_create_member("Someone Else", "0700000001")
        self.assertEqual(outcome, "created")
        self.assertNotEqual(found.name, "Jane Doe")


class MemberDetailPhoneUiTests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.client.force_login(self.tr)

    def test_secondary_numbers_shown_on_detail_page(self):
        m1 = Member.objects.create(name="UI Test", phone="0712345678")
        m2 = Member.objects.create(name="UI Test", phone="0798765432")
        kept = merge_members(m1, m2)
        r = self.client.get(reverse("member_detail", args=[kept.pk]))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Other phone numbers", html)

    def test_primary_only_member_shows_no_secondary_section(self):
        m = Member.objects.create(name="Single Number", phone="0712345678")
        r = self.client.get(reverse("member_detail", args=[m.pk]))
        html = r.content.decode()
        self.assertNotIn("Other phone numbers", html)
