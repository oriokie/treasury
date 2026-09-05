"""Server-rendered PNG of development-group collections on the leader
department page — same Pillow table approach as fund budget PNGs, scoped to
the selected start/end period."""
import datetime as dt
import io
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase, Client
from PIL import Image

from departments.models import Department, DepartmentLeadership, DevelopmentGroup
from giving.models import Transaction


def _leader(dept, name="ld_devpng"):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name="Leader")[0])
    DepartmentLeadership.objects.create(department=dept, user=u)
    return u


class DevGroupCollectionsPngTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="Dev PNG Fund", fund_type="LOCAL", category="DEVELOPMENT")
        self.leader = _leader(self.dept)
        self.g = DevelopmentGroup.objects.create(
            number=1, name="Group One", target=Decimal("10000"), active=True)
        Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="CASH", direction="CREDIT",
            amount=Decimal("2500"), department=self.dept, dev_group=self.g,
            payer_name="Giver", confirmed=True, allocation_status="MANUAL",
            core_ref="PNG1")
        self.c = Client()
        self.c.force_login(self.leader)

    def test_builder_returns_valid_png(self):
        from cashbook.services.goal_chart import (
            build_dev_group_collections_png, SCALE)
        data = build_dev_group_collections_png(
            dept_name=self.dept.name,
            start=dt.date(2026, 6, 1), end=dt.date(2026, 6, 30),
            rows=[{"name": "Group One", "opening": Decimal("0"),
                   "collected": Decimal("2500"), "closing": Decimal("2500")}],
            church_name="Test Church")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        img = Image.open(io.BytesIO(data))
        self.assertEqual(img.format, "PNG")
        self.assertEqual(img.width % SCALE, 0)
        self.assertLessEqual(img.width // SCALE, 800)

    def test_export_endpoint_returns_png_for_period(self):
        url = (f"/leader/department/{self.dept.id}/"
               "?start=2026-06-01&end=2026-06-30&export=groups_png")
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")
        self.assertIn(".png", r["Content-Disposition"])
        self.assertTrue(r.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_department_page_links_and_embeds_png(self):
        r = self.c.get(
            f"/leader/department/{self.dept.id}/"
            "?start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("export=groups_png", body)
        self.assertIn("⤓ PNG", body)
        self.assertIn("Show image", body)
        self.assertIn('class="ld-dev-groups-png"', body)

    def test_non_leader_cannot_download(self):
        other = User.objects.create_user("outsider", password="x")
        self.c.force_login(other)
        r = self.c.get(
            f"/leader/department/{self.dept.id}/"
            "?start=2026-06-01&end=2026-06-30&export=groups_png")
        self.assertIn(r.status_code, (302, 403))
