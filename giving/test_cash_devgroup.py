"""Selecting a DEVELOPMENT fund on the cash form requires a development group."""
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department, DevelopmentGroup
from giving.models import Transaction


class CashDevGroupTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("cdg", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)
        self.dev = Department.objects.create(name="Dev Fund", fund_type="LOCAL",
                                             category="DEVELOPMENT", selectable=True)
        self.g = DevelopmentGroup.objects.create(number=1, name="Group 1")

    def test_development_without_group_rejected(self):
        self.c.post("/cash/new/", {"date": "2026-06-10", "channel": "CASH",
                                   "fund": f"d:{self.dev.id}", "amount": "500"})
        self.assertFalse(Transaction.objects.filter(department=self.dev).exists())

    def test_development_with_group_saved(self):
        self.c.post("/cash/new/", {"date": "2026-06-10", "channel": "CASH",
                                   "fund": f"d:{self.dev.id}", "amount": "500",
                                   "dev_group": str(self.g.id)})
        t = Transaction.objects.get(department=self.dev, amount=500)
        self.assertEqual(t.dev_group_id, self.g.id)

    def test_fund_search_flags_development(self):
        import json
        r = self.c.get(f"/funds/search/?scope=income&q={self.dev.name[:3]}")
        data = json.loads(r.content)
        self.assertTrue(any(x.get("dev") for x in data["results"]))
