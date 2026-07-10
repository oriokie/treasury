"""Server-side JPEG chart download for the Group Contribution Goals section."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction


def _tr():
    u = User.objects.create_user("tr_ggjpg", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class GroupGoalsJpegTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.exp = Department.objects.create(name="JpegCampV2", fund_type="LOCAL",
            category="MINISTRY", goal_type="CAMP_EXPENSE", year_goal=Decimal("730000"))
        for i in range(3):
            g = Department.objects.create(name=f"JpegGroup{i}", fund_type="LOCAL",
                category="MINISTRY", parent=self.exp, contribution_goal=Decimal("35000"))
            Transaction.objects.create(date=dt.date(2026, 6, 10),
                amount=Decimal(str(10000 * (i + 1))), direction="CREDIT", confirmed=True,
                channel="CASH", allocation_status="MANUAL", department=g)
        self.c = Client(); self.c.force_login(self.tr)

    def test_jpeg_download_status_and_type(self):
        r = self.c.get(f"/reports/fund/{self.exp.id}/budget/group-goals.jpg?year=2026")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/jpeg")

    def test_jpeg_is_valid_image(self):
        import io
        from PIL import Image
        r = self.c.get(f"/reports/fund/{self.exp.id}/budget/group-goals.jpg?year=2026")
        img = Image.open(io.BytesIO(r.content))
        self.assertEqual(img.format, "JPEG")
        self.assertGreater(img.size[0], 0)
        self.assertGreater(img.size[1], 0)

    def test_download_button_on_budget_page(self):
        b = self.c.get(f"/reports/fund/{self.exp.id}/budget/?year=2026").content.decode()
        self.assertIn("group-goals.jpg", b)

    def test_no_button_when_no_groups(self):
        plain = Department.objects.create(name="NoGroupsFund", fund_type="LOCAL",
            category="MINISTRY")
        b = self.c.get(f"/reports/fund/{plain.id}/budget/?year=2026").content.decode()
        self.assertNotIn("group-goals.jpg", b)
