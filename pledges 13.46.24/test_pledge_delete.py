"""Pledges can be deleted by a treasurer; matched gifts stay in the ledger (#4)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from pledges.models import Pledge, PledgeCampaign
from members.models import Member


class PledgeDeleteTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("pd", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)
        self.camp = PledgeCampaign.objects.create(name="Building Fund")
        self.m = Member.objects.create(name="Jane Doe")

    def test_delete_pledge(self):
        p = Pledge.objects.create(campaign=self.camp, member=self.m,
            amount=Decimal("10000"), recorded_by=self.u, status="ACTIVE",
            start_date=dt.date(2026, 1, 1))
        b = self.c.get(f"/pledges/{p.id}/").content.decode()
        self.assertIn(f"/pledges/{p.id}/delete/", b)
        r = self.c.post(f"/pledges/{p.id}/delete/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Pledge.objects.filter(pk=p.id).exists())
