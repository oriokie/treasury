"""The Expense Register's fund (department) filter.

The view has always honoured `?department=<id>`; until now no control on the
page produced it, so the filter was reachable only by typing a URL. These tests
cover the control itself and the two decisions behind it: the dropdown is
derived from the register's own rows, so a fund CLOSED after its spending
happened stays filterable, and a fund that has never been spent from is never
offered.
"""
import datetime as dt
import re
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from cashbook.models import Expense


def _select_html(content):
    """Just the fund <select>, so assertions can't be satisfied by the fund
    name appearing somewhere else on the page (a table row, for instance)."""
    m = re.search(r'<select name="department".*?</select>', content, re.S)
    return m.group(0) if m else ""


class ExpenseFundFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.treasurer = User.objects.create_user("eff_tr", password="x")
        cls.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])

        cls.youth = Department.objects.create(name="Youth", fund_type="LOCAL",
                                              category="MINISTRY")
        cls.choir = Department.objects.create(name="Choir", fund_type="LOCAL",
                                              category="MINISTRY", parent=cls.youth)
        cls.music = Department.objects.create(name="Music", fund_type="LOCAL",
                                              category="MINISTRY")
        # spent from, then closed — its history must stay reachable
        cls.camp = Department.objects.create(name="Camp 2024", fund_type="LOCAL",
                                             category="DEVELOPMENT")
        # never spent from
        cls.unused = Department.objects.create(name="Unused Fund", fund_type="LOCAL",
                                               category="MINISTRY")

        day = dt.date(2024, 3, 4)
        for fund, desc, amount in (
            (cls.youth, "Youth outing", "100"),
            (cls.youth, "Youth banner", "150"),
            (cls.choir, "Choir robes", "200"),
            (cls.music, "Keyboard stand", "300"),
            (cls.camp, "Camp firewood", "400"),
        ):
            Expense.objects.create(date=day, department=fund, description=desc,
                                   amount=Decimal(amount), category="OTHER",
                                   status="PAID", recorded_by=cls.treasurer)

        cls.camp.status = Department.Status.CLOSED
        cls.camp.save()

    def setUp(self):
        self.client.force_login(self.treasurer)
        self.url = reverse("expense_list")

    def test_the_page_offers_a_fund_dropdown(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('<select name="department"', html,
                      "the Expense Register toolbar has no fund filter control")

    def test_every_fund_with_expenses_is_offered(self):
        sel = _select_html(self.client.get(self.url).content.decode())
        for fund in (self.youth, self.music, self.camp):
            self.assertIn(f'value="{fund.id}"', sel,
                          f"{fund.name} has expenses but is not in the dropdown")

    def test_a_closed_fund_with_history_is_still_filterable(self):
        """Closed/archived accounts 'stay in historical reports' — so their
        spending must stay reachable from the filter that lists it."""
        self.assertFalse(self.camp.active)
        sel = _select_html(self.client.get(self.url).content.decode())
        self.assertIn(f'value="{self.camp.id}"', sel,
                      "a fund closed after its spending happened dropped out of "
                      "the filter, stranding its expenses")

        rows = self.client.get(self.url, {"department": self.camp.id}).context["expenses"]
        self.assertEqual([e.description for e in rows], ["Camp firewood"])

    def test_a_fund_with_no_expenses_is_not_offered(self):
        sel = _select_html(self.client.get(self.url).content.decode())
        # assert the dropdown is really there first — otherwise this test's
        # point (absence) is satisfied by an empty string, and it would pass
        # just as happily on a page with no fund filter at all
        self.assertIn(f'value="{self.youth.id}"', sel, "no fund dropdown to check")
        self.assertNotIn(f'value="{self.unused.id}"', sel,
                         "the dropdown offers a fund with nothing to show")

    def test_options_are_ordered_the_way_they_read(self):
        """A sub-account renders as "Youth / Choir", so it must sort under
        Youth. The model's default ordering (fund_type, name) files it under
        C, which reads as an unsorted list to anyone scanning the dropdown."""
        sel = _select_html(self.client.get(self.url).content.decode())
        labels = re.findall(r'<option value="\d+"[^>]*>([^<]+)</option>', sel)
        self.assertEqual(labels, ["Camp 2024", "Music", "Youth", "Youth / Choir"])

    def test_selecting_a_fund_narrows_the_rows(self):
        rows = self.client.get(self.url, {"department": self.youth.id}).context["expenses"]
        self.assertEqual(sorted(e.description for e in rows),
                         ["Youth banner", "Youth outing"])

    def test_a_sub_account_filters_on_its_own_rows_only(self):
        """Youth / Choir is its own fund: choosing the parent does not sweep in
        the child, matching how the ledger's fund filter already behaves."""
        parent = self.client.get(self.url, {"department": self.youth.id}).context["expenses"]
        self.assertNotIn("Choir robes", [e.description for e in parent])
        child = self.client.get(self.url, {"department": self.choir.id}).context["expenses"]
        self.assertEqual([e.description for e in child], ["Choir robes"])

    def test_the_chosen_fund_stays_selected_on_the_page(self):
        sel = _select_html(
            self.client.get(self.url, {"department": self.music.id}).content.decode())
        self.assertRegex(sel, rf'value="{self.music.id}"\s+selected',
                         "the dropdown forgets which fund is being filtered on")

    def test_the_filtered_total_follows_the_fund(self):
        ctx = self.client.get(self.url, {"department": self.youth.id}).context
        self.assertEqual(ctx["filtered_total"], Decimal("250"))

    def test_the_status_bar_keeps_the_fund_when_switching_status(self):
        """The status chips carry the other active filters, so clicking one
        must not silently widen the page back to every fund."""
        ctx = self.client.get(self.url, {"department": self.youth.id}).context
        self.assertIn(f"department={self.youth.id}", ctx["status_qs"])
        paid = [s for s in ctx["status_bar"] if s["code"] == "PAID"][0]
        self.assertEqual(paid["t"], Decimal("250"))

    def test_the_export_respects_the_fund(self):
        body = self.client.get(
            self.url, {"department": self.youth.id, "export": "csv"}).content.decode()
        self.assertIn("Youth outing", body)
        self.assertNotIn("Keyboard stand", body)

    def test_clear_appears_when_only_a_fund_is_chosen(self):
        html = self.client.get(self.url, {"department": self.youth.id}).content.decode()
        self.assertIn("Clear</a>", html,
                      "a fund filter on its own offers no way to clear it")
