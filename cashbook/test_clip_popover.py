"""Attachment popover (paperclip) must stay hidden until clicked. Regression
for a bug where an unconditional `.clip-pop{display:flex}` CSS rule overrode
the browser's default `[hidden]{display:none}` styling, so every popover on
the expense list rendered open immediately, flooding the page with M-Pesa
text. Also checks the JS closes any other open popover so only one shows at
a time."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense, ExpenseAttachment


def _tr():
    u = User.objects.create_user("tr_clip", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class ClipPopoverHiddenTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="ClipF", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.e = Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Has receipt", amount=Decimal("500"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        ExpenseAttachment.objects.create(expense=self.e,
            text="QGH7X8 Confirmed. You have paid Ksh500 to X")
        self.c = Client(); self.c.force_login(self.tr)

    def test_popover_has_hidden_attribute(self):
        # explicit range: about the popover markup itself, not the list
        # view's bare-visit current-month default
        b = self.c.get("/expenses/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn('class="clip-pop" hidden', b)

    def test_css_forces_hidden_attribute_to_hide(self):
        css = open("static/css/app.css").read()
        self.assertIn(".clip-pop[hidden]{display:none}", css)

    def test_only_one_popover_open_at_a_time_js(self):
        b = self.c.get("/expenses/?start=2026-06-01&end=2026-06-30").content.decode()
        # opening one popover closes all others via querySelectorAll('.clip-pop')
        self.assertIn("document.querySelectorAll('.clip-pop').forEach", b)

    def test_outside_click_closes_popovers(self):
        b = self.c.get("/expenses/").content.decode()
        self.assertIn('closest(".clip-wrap")', b)
        self.assertIn('querySelectorAll(".clip-pop")', b)
