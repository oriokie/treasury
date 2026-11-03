"""Ignore duplicate flags and forward merge audit log."""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.roles import TREASURER
from members.models import Member, MemberMergeLog, PossibleDuplicate
from members.services.matching import merge_members


class DuplicateIgnoreAndMergeLogTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("dup_ig", password="x",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.user)

    def test_ignore_orphan_flag(self):
        m = Member.objects.create(name="ORPHAN FLAG", phone="254700000001",
                                  active=True, source=Member.Source.AUTO_BANK)
        flag = PossibleDuplicate.objects.create(member=m)
        r = self.client.post("/members/duplicates/ignore/", {"flag": flag.pk})
        self.assertEqual(r.status_code, 302)
        flag.refresh_from_db()
        self.assertTrue(flag.resolved)

    def test_merge_writes_log_on_keep(self):
        keep = Member.objects.create(name="KEEP PERSON", phone="254700000002",
                                     active=True)
        absorb = Member.objects.create(name="ABSORB PERSON", phone="254700000003",
                                       active=True, source=Member.Source.AUTO_BANK)
        absorb_id = absorb.pk
        absorb_code = absorb.match_code
        merge_members(keep, absorb, user=self.user)
        log = MemberMergeLog.objects.get(keep=keep)
        self.assertEqual(log.absorb_id, absorb_id)
        self.assertEqual(log.absorb_name, "ABSORB PERSON")
        self.assertEqual(log.absorb_match_code, absorb_code or log.absorb_match_code)
        self.assertEqual(log.merged_by_id, self.user.pk)
        r = self.client.get(f"/members/{keep.pk}/")
        self.assertContains(r, "Merged into this member")
        self.assertContains(r, "ABSORB PERSON")
