"""'Merge all clear matches' merged people the matcher had deliberately kept apart.

When a payment arrives under a name that already exists but from a DIFFERENT
established number, ``match_or_create_member`` refuses the name match, opens a
second record and raises a PossibleDuplicate flag. That refusal is the system
saying: same name, two numbers, quite possibly two people — someone should look.

The duplicate queue then looked at the name and nothing else. One same-name
record meant "exactly one candidate", which meant "unambiguous", which meant the
bulk endpoint merged the pair with no confirmation — throwing away the very
signal that had kept them apart. Two John Kamaus with two real numbers would
have their pledges, envelopes, loans and benevolent history permanently folded
into one record, and a merge cannot be undone.

These tests build the pair through the real matching path rather than by hand,
so they also fail if the matcher's own rule about phones ever moves: the queue
and the matcher have to mean the same thing by "the same person".
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from members.models import Member, PossibleDuplicate
from members.services.matching import match_or_create_member


class DuplicateQueueFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_dupphone", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

    def flagged_pair_with_clashing_phones(self, name="John Kamau"):
        """Two same-name records with different numbers, created the way the
        live system creates them: an existing member, then a bank payment under
        the same name from another line."""
        existing = Member.objects.create(name=name, phone="0722111222")
        flagged, outcome = match_or_create_member(name, "0733444555")
        # the premise of every test below — if this ever stops holding, the
        # matcher's rule changed and the queue's rule must be revisited with it
        self.assertEqual(outcome, "created")
        self.assertNotEqual(flagged.pk, existing.pk)
        self.assertTrue(PossibleDuplicate.objects.filter(
            member=flagged, resolved=False).exists())
        return existing, flagged


class BulkMergeRespectsThePhoneMismatchTests(DuplicateQueueFixture):

    def test_a_pair_the_matcher_refused_to_match_is_not_merged_in_bulk(self):
        existing, flagged = self.flagged_pair_with_clashing_phones()

        resp = self.client.post(reverse("member_bulk_merge"), follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Member.objects.filter(pk=existing.pk).exists())
        self.assertTrue(Member.objects.filter(pk=flagged.pk).exists())
        # and the flag stays open: the pair still needs a person, so it must not
        # disappear from the review queue either
        self.assertTrue(PossibleDuplicate.objects.filter(
            member=flagged, resolved=False).exists())

    def test_the_treasurer_is_told_why_the_queue_did_not_empty(self):
        """A pair silently left behind looks like a broken button; the reason
        has to reach the person who pressed it."""
        self.flagged_pair_with_clashing_phones()

        resp = self.client.post(reverse("member_bulk_merge"), follow=True)

        text = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("different phone numbers", text)

    def test_a_clean_pair_in_the_same_run_is_still_merged(self):
        """Withholding one pair must not turn the bulk action off."""
        self.flagged_pair_with_clashing_phones("John Kamau")
        clean = Member.objects.create(name="Grace Atieno", source=Member.Source.MANUAL)
        clean_dup = Member.objects.create(name="Grace Atieno",
                                          source=Member.Source.AUTO_BANK)
        PossibleDuplicate.objects.create(member=clean_dup)

        self.client.post(reverse("member_bulk_merge"), follow=True)

        self.assertEqual(Member.objects.filter(name_key="JOHN KAMAU").count(), 2)
        self.assertFalse(Member.objects.filter(pk=clean_dup.pk).exists())
        self.assertTrue(Member.objects.filter(pk=clean.pk).exists())

    def test_a_candidate_with_no_phone_on_file_is_still_merged_unattended(self):
        """No number is not a conflicting number. ``match_or_create_member``
        matches a phoneless member by name and adopts the incoming number, so
        the queue must not become more suspicious than the matcher itself."""
        existing = Member.objects.create(name="Peter Mwangi",
                                         source=Member.Source.MANUAL)
        flagged = Member.objects.create(name="Peter Mwangi", phone="0700111000",
                                        source=Member.Source.AUTO_BANK)
        PossibleDuplicate.objects.create(member=flagged)

        self.client.post(reverse("member_bulk_merge"), follow=True)

        self.assertTrue(Member.objects.filter(pk=existing.pk).exists())
        self.assertFalse(Member.objects.filter(pk=flagged.pk).exists())

    def test_one_number_written_two_ways_is_the_same_number_not_a_conflict(self):
        """The comparison normalises before deciding, using the matcher's own
        ``normalize_phone``. A row stored before numbers were normalised holds
        '0712345678' where a fresh one holds '254712345678'; those are one
        number, and treating them as a disagreement would strand a genuine
        duplicate in the queue forever."""
        existing = Member.objects.create(name="Ruth Momanyi", phone="254712345678",
                                         source=Member.Source.MANUAL)
        # .update() writes past Member.save(), which is exactly how a legacy row
        # comes to hold the local form
        Member.objects.filter(pk=existing.pk).update(phone="0712345678")
        flagged = Member.objects.create(name="Momanyi Ruth", phone="254712345678",
                                        source=Member.Source.AUTO_BANK)
        PossibleDuplicate.objects.create(member=flagged)

        self.client.post(reverse("member_bulk_merge"), follow=True)

        self.assertTrue(Member.objects.filter(pk=existing.pk).exists())
        self.assertFalse(Member.objects.filter(pk=flagged.pk).exists())


class DuplicateReviewScreenTests(DuplicateQueueFixture):

    def test_the_review_screen_does_not_call_a_phone_mismatch_a_clear_match(self):
        """`single` is what the page turns into a bare 'Merge these two' button
        and into the count on 'Merge all N clear matches'. A pair the bulk
        endpoint now withholds must not be advertised there as one click's
        work — the page and the endpoint have to agree."""
        self.flagged_pair_with_clashing_phones()

        resp = self.client.get(reverse("member_duplicates"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["single_count"], 0)
        row = resp.context["rows"][0]
        self.assertFalse(row["single"])

    def test_the_pair_is_still_offered_for_a_deliberate_merge(self):
        """Withheld from the one-click path, not hidden: if they really are one
        person the treasurer must still be able to say so, with both numbers in
        front of them."""
        existing, flagged = self.flagged_pair_with_clashing_phones()

        resp = self.client.get(reverse("member_duplicates"))
        row = resp.context["rows"][0]
        self.assertEqual([c.pk for c in row["candidates"]], [existing.pk])

        self.client.post(reverse("member_merge"),
                         {"keep": existing.pk, "absorb": flagged.pk}, follow=True)
        self.assertFalse(Member.objects.filter(pk=flagged.pk).exists())
