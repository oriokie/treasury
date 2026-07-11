"""Server-side chart download for the Group Contribution Goals section.

NOTE (fixed during the Benevolent Phase 1 review): this file was written when the
download was a JPEG and was never updated when the feature moved to PNG (the
route is `group-goals.png`, served by GroupGoalsPngView — see the v2.44 notes on
those views). Every assertion here was therefore hitting a 404 and failing:
`Image.open` on a 404 body, and the button assertions looking for a filename the
template no longer emits. The route was never broken — only the test was stale.
The filename is kept so no test path references break.
"""
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

    def test_chart_download_status_and_type(self):
        r = self.c.get(f"/reports/fund/{self.exp.id}/budget/group-goals.png?year=2026")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")

    def test_chart_is_a_valid_image(self):
        import io
        from PIL import Image
        r = self.c.get(f"/reports/fund/{self.exp.id}/budget/group-goals.png?year=2026")
        img = Image.open(io.BytesIO(r.content))
        self.assertEqual(img.format, "PNG")
        self.assertGreater(img.size[0], 0)
        self.assertGreater(img.size[1], 0)

    def test_download_button_on_budget_page(self):
        b = self.c.get(f"/reports/fund/{self.exp.id}/budget/?year=2026").content.decode()
        self.assertIn("group-goals.png", b)

    def test_no_button_when_no_groups(self):
        plain = Department.objects.create(name="NoGroupsFund", fund_type="LOCAL",
            category="MINISTRY")
        b = self.c.get(f"/reports/fund/{plain.id}/budget/?year=2026").content.decode()
        self.assertNotIn("group-goals.png", b)
