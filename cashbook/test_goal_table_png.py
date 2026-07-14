"""The Group Contribution Goals image is rendered as an actual table
(Group / Goal / Collected <year> / To go / Progress), matching the on-screen
HTML table's exact columns, instead of a progress-bar-only chart — using
server-side Pillow generation. PNG rather than JPEG: this is a table of sharp
text and flat fills, and PNG's lossless compression keeps it crisp instead of
JPEG-blurred (v2.40 renamed this from JPEG to PNG for exactly that reason)."""
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department


def _tr():
    u = User.objects.create_user("tr_goaltable", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class GoalTablePngTests(TestCase):
    def test_generates_valid_png(self):
        from cashbook.services.goal_chart import build_group_goals_png
        group_rows = [
            {"name": "Group 1", "goal": Decimal("50000"), "collected": Decimal("32000"),
             "pct": 64, "short": Decimal("18000")},
            {"name": "Group 2", "goal": Decimal("30000"), "collected": Decimal("35000"),
             "pct": 100, "short": Decimal("0")},
        ]
        contribution_goal = {"goal": Decimal("80000"), "collected": Decimal("67000"),
                             "short": Decimal("13000")}
        data = build_group_goals_png(dept_name="Camp Meeting", year=2026,
            group_rows=group_rows, contribution_goal=contribution_goal,
            church_name="Test Church")
        self.assertGreater(len(data), 0)
        import io
        from PIL import Image
        from cashbook.services.goal_chart import SCALE
        img = Image.open(io.BytesIO(data))
        self.assertEqual(img.format, "PNG")
        # Rendered at 4x logical size for print quality.
        #
        # This used to assert `img.width == 1180 * 4` — and that 1180 WAS the bug
        # reported: a phone scales a 1180px-wide image to fit a ~380px viewport,
        # about a third, and every piece of text in it shrinks by the same third.
        # The complaint was "the fonts are too small"; the cause was that the
        # image was a desktop table being shrunk.
        #
        # So this now asserts the requirement rather than the old number: narrow
        # enough that a phone does not have to shrink it into illegibility.
        logical_width = img.width // SCALE
        self.assertLessEqual(
            logical_width, 800,
            "a wider image is scaled down further on a phone, taking the text with "
            "it — the width IS the font-size problem")
        self.assertEqual(img.width, logical_width * SCALE)

    def test_empty_group_rows_still_renders(self):
        from cashbook.services.goal_chart import build_group_goals_png
        data = build_group_goals_png(dept_name="Empty Fund", year=2026,
            group_rows=[], contribution_goal={"goal": 0, "collected": 0, "short": 0})
        self.assertGreater(len(data), 0)

    def test_met_goal_row_does_not_crash(self):
        from cashbook.services.goal_chart import build_group_goals_png
        group_rows = [{"name": "Overachiever", "goal": Decimal("1000"),
                      "collected": Decimal("1500"), "pct": 150, "short": Decimal("0")}]
        data = build_group_goals_png(dept_name="Test", year=2026,
            group_rows=group_rows, contribution_goal={"goal": 1000, "collected": 1500,
            "short": 0})
        self.assertGreater(len(data), 0)

    def test_zero_goal_does_not_divide_by_zero(self):
        from cashbook.services.goal_chart import build_group_goals_png
        group_rows = [{"name": "No Goal Set", "goal": Decimal("0"),
                      "collected": Decimal("500"), "pct": 0, "short": Decimal("0")}]
        data = build_group_goals_png(dept_name="Test", year=2026,
            group_rows=group_rows, contribution_goal={"goal": 0, "collected": 500, "short": 0})
        self.assertGreater(len(data), 0)

    def test_view_endpoint_returns_png(self):
        tr = _tr()
        d = Department.objects.create(name="TablePngFund", fund_type="LOCAL",
            category="DEVELOPMENT", contribution_goal=Decimal("10000"))
        Department.objects.create(name="Group A", fund_type="LOCAL",
            category="DEVELOPMENT", parent=d, contribution_goal=Decimal("5000"))
        c = Client(); c.force_login(tr)
        r = c.get(f"/reports/fund/{d.id}/budget/group-goals.png?year=2026")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")
        self.assertIn(".png", r["Content-Disposition"])

    def test_html_page_links_to_png_download(self):
        tr = _tr()
        d = Department.objects.create(name="TablePngLinkFund", fund_type="LOCAL",
            category="DEVELOPMENT")
        Department.objects.create(name="Group B", fund_type="LOCAL",
            category="DEVELOPMENT", parent=d, contribution_goal=Decimal("3000"))
        c = Client(); c.force_login(tr)
        b = c.get(f"/reports/fund/{d.id}/budget/").content.decode()
        self.assertIn("group-goals.png", b)
        self.assertNotIn("group-goals.jpg", b)
