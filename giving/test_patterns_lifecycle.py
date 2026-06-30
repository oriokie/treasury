"""#8 configurable dev-group patterns + #9 allocation-rule lifecycle."""
import datetime as dt
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import AllocationRule, DevGroupPattern
from giving.services.allocation import allocate, clear_pattern_cache


def _treasurer():
    u = User.objects.create_user("tr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class DevPatternTests(TestCase):
    def setUp(self):
        # seed defaults (migrations may not run data-seed in test fixtures reliably)
        DevGroupPattern.objects.get_or_create(label="num", defaults=dict(
            pattern=r"(?:dev(?:e?l?o?p?)?(?:gr(?:ou)?p?|gp|g)?|gr(?:ou)?p|gp)0*(\d+)",
            kind="NUMBERED", sort_order=10))
        DevGroupPattern.objects.get_or_create(label="word", defaults=dict(
            pattern=r"(?:dev(?:elop)?|grp|group|gp)", kind="WORD", sort_order=20))
        clear_pattern_cache()
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)

    def test_default_patterns_match(self):
        self.assertEqual(allocate("DEVGR7")[0], "DEV_GROUP_7")
        self.assertEqual(allocate("dev grp 11")[0], "DEV_GROUP_11")
        self.assertEqual(allocate("development")[0], "DEV_GROUP_NA")

    def test_invalid_regex_rejected(self):
        before = DevGroupPattern.objects.count()
        self.c.post("/dev-patterns/", {"label": "bad", "pattern": "(unclosed",
                                       "kind": "WORD", "sort_order": "9"})
        self.assertEqual(DevGroupPattern.objects.count(), before)

    def test_numbered_requires_capture_group(self):
        before = DevGroupPattern.objects.count()
        self.c.post("/dev-patterns/", {"label": "nogroup", "pattern": "dev",
                                       "kind": "NUMBERED", "sort_order": "9"})
        self.assertEqual(DevGroupPattern.objects.count(), before)

    def test_add_and_toggle_pattern(self):
        self.c.post("/dev-patterns/", {"label": "ka", "pattern": r"ka0*(\d+)",
            "kind": "NUMBERED", "sort_order": "5", "enabled": "on"})
        clear_pattern_cache()
        self.assertEqual(allocate("KA12")[0], "DEV_GROUP_12")
        p = DevGroupPattern.objects.get(label="ka")
        self.c.post(f"/dev-patterns/{p.id}/toggle/", {})
        clear_pattern_cache()
        self.assertNotEqual(allocate("KA12")[0], "DEV_GROUP_12")

    def test_live_tester(self):
        body = self.c.get("/dev-patterns/?test=DEVGR39").content.decode()
        self.assertIn("development group 39", body)


class RuleLifecycleTests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="Camp", fund_type="LOCAL",
                                           category="MINISTRY")

    def test_expired_property(self):
        r = AllocationRule.objects.create(reference="t24", department=self.d,
            valid_from=dt.date(2024, 1, 1), valid_to=dt.date(2024, 12, 31))
        self.assertTrue(r.is_expired)

    def test_archive_excludes_from_allocation(self):
        r = AllocationRule.objects.create(reference="zzz", department=self.d,
                                          source="SEED")
        self.assertEqual(allocate("zzz")[0], self.d)
        r.archive()
        self.assertNotEqual(allocate("zzz")[0], self.d)

    def test_bulk_archive_expired(self):
        AllocationRule.objects.create(reference="old", department=self.d,
            valid_to=dt.date(2024, 1, 1))
        self.c.post("/rules/archive-expired/", {})
        self.assertTrue(AllocationRule.objects.get(reference="old").archived)

    def test_restore(self):
        r = AllocationRule.objects.create(reference="r", department=self.d)
        r.archive()
        self.c.post(f"/rules/{r.id}/archive/", {"action": "restore"})
        r.refresh_from_db()
        self.assertFalse(r.archived)

    def test_command_grace(self):
        from io import StringIO
        from django.core.management import call_command
        AllocationRule.objects.create(reference="g", department=self.d,
            valid_to=dt.date(2024, 1, 1))
        out = StringIO()
        call_command("archive_expired_rules", "--grace", "30", stdout=out)
        self.assertTrue(AllocationRule.objects.get(reference="g").archived)


class FontFamilyTests(TestCase):
    def test_font_family_persists_and_applies(self):
        u = User.objects.create_user("ff", password="x")
        c = Client(); c.force_login(u)
        c.post("/preferences/update/", {"key": "font_family", "value": "SERIF"})
        u.refresh_from_db()
        self.assertEqual(u.preference.font_family, "SERIF")
        self.assertIn('data-fontfamily="serif"', c.get("/").content.decode())


class BalancingTests(TestCase):
    def test_balances_size_and_total(self):
        from decimal import Decimal
        from reports.views import _balanced_partition
        items = [(i, f"M{i}", "", Decimal(str((i % 5 + 1) * 100))) for i in range(40)]
        buckets = _balanced_partition(items, 5)
        sizes = [len(b["members"]) for b in buckets]
        self.assertLessEqual(max(sizes) - min(sizes), 1)
        totals = [b["total"] for b in buckets]
        avg = sum(totals) / 5
        self.assertLess(max(totals) - min(totals), avg)  # tight spread

    def test_whale_distributed(self):
        from decimal import Decimal
        from reports.views import _balanced_partition
        items = [(0, "Whale", "", Decimal("9000"))] + \
                [(i, f"S{i}", "", Decimal("100")) for i in range(1, 11)]
        buckets = _balanced_partition(items, 3)
        sizes = [len(b["members"]) for b in buckets]
        self.assertLessEqual(max(sizes) - min(sizes), 1)
        placed = sorted(m["id"] for b in buckets for m in b["members"])
        self.assertEqual(placed, list(range(11)))
