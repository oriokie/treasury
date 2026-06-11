from django.test import TestCase

from departments.models import Department
from members.models import Member, MemberAlias, normalize_phone, name_key
from members.services.matching import match_or_create_member, merge_members
from giving.models import Transaction


class PhoneNormalizationTests(TestCase):
    def test_local_zero_prefix(self):
        self.assertEqual(normalize_phone("0790301470"), "254790301470")

    def test_nine_digit(self):
        self.assertEqual(normalize_phone("790301470"), "254790301470")

    def test_already_international(self):
        self.assertEqual(normalize_phone("254790301470"), "254790301470")

    def test_spaces_and_plus(self):
        self.assertEqual(normalize_phone("+254 790 301 470"), "254790301470")

    def test_implausible_returns_none(self):
        self.assertIsNone(normalize_phone("12345"))
        self.assertIsNone(normalize_phone(""))


class NameKeyTests(TestCase):
    def test_order_insensitive(self):
        self.assertEqual(name_key("RUTH MOMANYI"), name_key("MOMANYI RUTH"))

    def test_case_and_punctuation(self):
        self.assertEqual(name_key("Edwin  Orioki"), name_key("edwin, orioki"))


class MemberMatchingTests(TestCase):
    def setUp(self):
        self.existing = Member.objects.create(
            name="Kevin Ogega", phone="0790301470", group=Member.Group.YOUTH)

    def test_match_by_phone_even_with_new_spelling(self):
        m, outcome = match_or_create_member("K. Ogega", "254790301470")
        self.assertEqual(m.pk, self.existing.pk)
        self.assertEqual(outcome, "matched_phone")

    def test_match_by_name_when_no_phone(self):
        m, outcome = match_or_create_member("Ogega Kevin", None)
        self.assertEqual(m.pk, self.existing.pk)
        self.assertEqual(outcome, "matched_name")

    def test_match_by_alias(self):
        MemberAlias.objects.create(member=self.existing, name="Kevo the Youth")
        m, outcome = match_or_create_member("Kevo the Youth", None)
        self.assertEqual(m.pk, self.existing.pk)

    def test_creates_when_unknown(self):
        m, outcome = match_or_create_member("Brand New Person", "0700000001")
        self.assertEqual(outcome, "created")
        self.assertEqual(m.source, Member.Source.AUTO_BANK)
        self.assertNotEqual(m.pk, self.existing.pk)

    def test_never_orphans_gift(self):
        m, _ = match_or_create_member("", "")
        self.assertIsNotNone(m)


class MergeTests(TestCase):
    def test_merge_moves_transactions_and_aliases(self):
        dept = Department.objects.create(name="Tithe", fund_type=Department.FundType.TRUST)
        keep = Member.objects.create(name="Mary Achieng", phone="0744555666")
        absorb = Member.objects.create(name="Maria Atieno", source=Member.Source.AUTO_BANK)
        Transaction.objects.create(
            date="2026-06-06", channel=Transaction.Channel.BANK,
            direction=Transaction.Direction.CREDIT, amount=100,
            department=dept, member=absorb)
        merge_members(keep, absorb)
        self.assertFalse(Member.objects.filter(pk=absorb.pk).exists())
        self.assertEqual(Transaction.objects.filter(member=keep).count(), 1)
        self.assertTrue(keep.aliases.filter(name="Maria Atieno").exists())

    def test_merge_same_name_order_skips_redundant_alias(self):
        keep = Member.objects.create(name="Mary Achieng", phone="0744555666")
        absorb = Member.objects.create(name="Achieng Mary", source=Member.Source.AUTO_BANK)
        merge_members(keep, absorb)
        # same name_key, so no alias is needed
        self.assertEqual(keep.aliases.count(), 0)


class MemberPhoneBackfillTests(TestCase):
    """Phones parsed from the bank narration onto transactions should reach the
    member, and orphan bank gifts should be linked to a matched/created member."""

    def test_backfill_fills_blank_phone(self):
        from django.core.management import call_command
        from members.models import Member
        from giving.models import Transaction
        import datetime as dt
        from decimal import Decimal
        m = Member.objects.create(name="No Phone", source="AUTO_BANK")
        Transaction.objects.create(
            date=dt.date(2026, 6, 6), channel="BANK", direction="CREDIT",
            amount=Decimal("100"), allocation_status="MANUAL", confirmed=True,
            member=m, payer_name="No Phone", payer_phone="254712345678",
            core_ref="BF1")
        call_command("backfill_member_phones", verbosity=0)
        m.refresh_from_db()
        self.assertEqual(m.phone, "254712345678")

    def test_backfill_links_orphan_gift(self):
        from django.core.management import call_command
        from giving.models import Transaction
        import datetime as dt
        from decimal import Decimal
        Transaction.objects.create(
            date=dt.date(2026, 6, 6), channel="BANK", direction="CREDIT",
            amount=Decimal("50"), allocation_status="MANUAL", confirmed=True,
            member=None, payer_name="Orphan", payer_phone="254798765432",
            core_ref="BF2")
        call_command("backfill_member_phones", verbosity=0)
        t = Transaction.objects.get(core_ref="BF2")
        self.assertIsNotNone(t.member)
        self.assertEqual(t.member.phone, "254798765432")


class MergeKeepsBothPhonesTests(TestCase):
    """Merging two records for the same person preserves both phone numbers,
    with one marked primary for receipting."""

    def test_merge_preserves_both_numbers(self):
        from members.models import Member
        from members.services.matching import merge_members
        keep = Member.objects.create(name="Mary Atieno", phone="254712000001", source="MANUAL")
        absorb = Member.objects.create(name="Mary Atieno", phone="254722000002", source="AUTO_BANK")
        merge_members(keep, absorb)
        keep.refresh_from_db()
        nums = set(keep.phones.values_list("number", flat=True))
        self.assertEqual(nums, {"254712000001", "254722000002"})
        self.assertEqual(keep.phones.filter(is_primary=True).count(), 1)
        self.assertEqual(keep.receipt_phone, "254712000001")  # keep's own stays primary
        self.assertFalse(Member.objects.filter(pk=absorb.pk).exists())


class BulkMergeTests(TestCase):
    """The bulk-merge action merges every duplicate with exactly one candidate,
    and leaves ambiguous ones alone."""

    def test_bulk_merges_single_candidates_only(self):
        from django.contrib.auth.models import User
        from django.test import Client
        from members.models import Member, PossibleDuplicate
        u = User.objects.create_user("t", password="x")
        from django.contrib.auth.models import Group
        g, _ = Group.objects.get_or_create(name="Treasurer")
        u.groups.add(g)
        # one clear duplicate (1 candidate)
        a1 = Member.objects.create(name="Paul Kim", source="MANUAL")
        a2 = Member.objects.create(name="Paul Kim", source="AUTO_BANK")
        PossibleDuplicate.objects.create(member=a2)
        # ambiguous (2 candidates) — should NOT be auto-merged
        b1 = Member.objects.create(name="Jane Doe", source="MANUAL")
        b2 = Member.objects.create(name="Jane Doe", source="MANUAL")
        b3 = Member.objects.create(name="Jane Doe", source="AUTO_BANK")
        PossibleDuplicate.objects.create(member=b3)
        c = Client(); c.force_login(u)
        c.post("/members/duplicates/merge-all/")
        # Paul Kim collapsed to one; Jane Doe still three
        self.assertEqual(Member.objects.filter(name="Paul Kim").count(), 1)
        self.assertEqual(Member.objects.filter(name="Jane Doe").count(), 3)
