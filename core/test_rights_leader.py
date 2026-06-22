"""Leader phone/identity display honours profile rights (#3). By default a leader
sees giver names but masked phones; a profile can grant the phone right (the bug
that was reported) or restrict the identity right."""
from django.test import TestCase
from django.contrib.auth.models import User, Group

from accounts.models import Profile
from core.rights import display_phone, display_giver


class LeaderRightsTests(TestCase):
    def setUp(self):
        self.lead = User.objects.create_user("ld", password="x")
        self.lead.groups.add(Group.objects.get_or_create(name="Leader")[0])

    def _u(self):
        return User.objects.get(pk=self.lead.pk)

    def test_default_leader_sees_name_but_masked_phone(self):
        self.assertNotEqual(display_phone(self.lead, "254712345678"), "254712345678")
        self.assertEqual(display_giver(self.lead, "Jane Doe"), "Jane Doe")

    def test_profile_grants_full_phone(self):
        p = Profile.objects.create(name="Full",
            rights=["view_member_phone_full", "view_giver_identity"])
        p.users.add(self.lead)
        u = self._u()
        self.assertEqual(display_phone(u, "254712345678"), "254712345678")
        self.assertEqual(display_giver(u, "Jane Doe"), "Jane Doe")

    def test_profile_can_restrict_identity(self):
        # a profile that grants only the phone right withholds identity
        p = Profile.objects.create(name="PhoneOnly", rights=["view_member_phone_full"])
        p.users.add(self.lead)
        u = self._u()
        self.assertEqual(display_phone(u, "254712345678"), "254712345678")
        self.assertEqual(display_giver(u, "Jane Doe"), "Giver (hidden)")
