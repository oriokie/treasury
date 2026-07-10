"""Executive overview drops the slow health alerts and shows quick facts (#4)."""
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group


class ExecutiveFactsTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("ex", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_no_alerts_has_facts(self):
        b = self.c.get("/executive/").content.decode()
        self.assertNotIn("Financial-health alerts", b)
        self.assertIn("At a glance", b)

    def test_quick_facts_callable(self):
        from core.services.dashboard import quick_facts
        facts = quick_facts()
        self.assertIsInstance(facts, list)
