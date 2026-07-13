"""Edwin asked two related questions:

  1. Does the Member model have other phone fields, and are they populated
     during a duplicate merge?
  2. When checking whether a member has contributed (e.g. the "not
     contributed to campaign" SMS criterion), does the system recognise a
     contribution made from ANY of a member's known phones, not just their
     primary one?

Both turn out to already be correctly implemented — MemberPhone exists,
merge_members() already consolidates every phone from both records, and
match_or_create_member() already checks phone OR phones__number. This file
proves that end to end rather than merely re-reading the code: a member
with a SECONDARY phone who gives from that line is correctly matched to
their own record, and is correctly recognised as "already contributed" by
the SMS criterion.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from members.models import Member, MemberPhone
from members.services.matching import match_or_create_member, merge_members


class SecondaryPhoneAuditFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tr_phoneaudit", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.fund = Department.objects.create(
            name="Phone Audit Fund", slug="phone-audit-fund",
            fund_type=Department.FundType.LOCAL, category=Department.Category.MINISTRY)


class MergePreservesEveryPhoneTests(SecondaryPhoneAuditFixture):

    def test_merge_keeps_both_records_phones_as_member_phone_rows(self):
        keep = Member.objects.create(name="Keep Member", phone="254711000001")
        absorb = Member.objects.create(name="Absorb Member", phone="254722000002")
        MemberPhone.objects.create(member=absorb, number="254733000003", label="work")

        merged = merge_members(keep, absorb)

        numbers = set(merged.phones.values_list("number", flat=True))
        self.assertEqual(numbers, {"254711000001", "254722000002", "254733000003"})

    def test_the_keep_members_own_phone_stays_primary(self):
        keep = Member.objects.create(name="Keep Member 2", phone="254711000004")
        absorb = Member.objects.create(name="Absorb Member 2", phone="254722000005")
        merged = merge_members(keep, absorb)
        primary = merged.phones.get(is_primary=True)
        self.assertEqual(primary.number, "254711000004")

    def test_a_payment_from_the_absorbed_members_old_number_still_finds_the_survivor(self):
        keep = Member.objects.create(name="Keep Member 3", phone="254711000006")
        absorb = Member.objects.create(name="Absorb Member 3", phone="254722000007")
        merge_members(keep, absorb)

        # a bank statement row referencing the OLD (absorbed) number must
        # still resolve to the surviving, merged record — not create a
        # brand new duplicate
        found, outcome = match_or_create_member("Anyone", "254722000007")
        self.assertEqual(found.pk, keep.pk)
        self.assertEqual(outcome, "matched_phone")


class SecondaryPhoneContributionMatchingTests(SecondaryPhoneAuditFixture):

    def test_a_gift_from_a_members_secondary_phone_is_attributed_to_them(self):
        member = Member.objects.create(name="Grace Achieng", phone="254711222000")
        MemberPhone.objects.create(member=member, number="254799888000", label="M-Pesa (spouse line)")

        found, outcome = match_or_create_member("Grace Achieng", "254799888000")
        self.assertEqual(found.pk, member.pk)
        self.assertEqual(outcome, "matched_phone")
        # and NOT a new, duplicate member
        self.assertEqual(Member.objects.filter(name__icontains="GRACE").count(), 1)

    def test_the_sms_not_contributed_criterion_recognises_a_secondary_phone_gift(self):
        from giving.models import Campaign, Transaction

        member = Member.objects.create(name="Peter Njenga", phone="254711333000",
                                       active=True)
        MemberPhone.objects.create(member=member, number="254788999000")
        camp = Campaign.objects.create(name="Camp Meeting 2026", department=self.fund)

        # give from the SECONDARY line — matched via match_or_create_member,
        # exactly as a real bank statement import would
        matched, _ = match_or_create_member("Peter Njenga", "254788999000")
        Transaction.objects.create(
            date=dt.date.today(), channel="BANK", direction="CREDIT",
            amount=Decimal("500"), department=self.fund, member=matched,
            confirmed=True, allocation_status="AUTO")

        r = self.client.get("/members/sms/", {
            "criteria": "not_contributed_campaign", "campaign": camp.pk})
        self.assertEqual(r.status_code, 200)
        recipients = r.context["recipients"] if "recipients" in getattr(r, "context", {}) else None
        body = r.content.decode()
        # the member gave (from their second line) and must NOT appear in
        # the "has not contributed" list
        self.assertNotIn("PETER NJENGA", body)

    def test_a_member_who_genuinely_has_not_given_still_appears(self):
        from giving.models import Campaign
        Member.objects.create(name="Never Gave", phone="254700111222", active=True)
        camp = Campaign.objects.create(name="Camp Meeting 2026 B", department=self.fund)
        r = self.client.get("/members/sms/", {
            "criteria": "not_contributed_campaign", "campaign": camp.pk})
        self.assertIn("NEVER GAVE", r.content.decode())
