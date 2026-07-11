"""Regression coverage for a real production bug: opening an existing
EnvelopeBatch whose rows use a fund OUTSIDE the "preferred" default set (see
envelopes.services.posting.PREFERRED) rendered that fund's column as hidden,
which zeroed the row's on-screen total against its saved "Total", flagged it
as a mismatch, and — because the grid's autosave wholesale-replaces a batch's
rows from whatever the browser can currently see — silently erased that
fund's amount from the database on the very next save.

The fix lives entirely in the inline JS on templates/envelopes/ledger.html
(there was nothing wrong server-side: EnvelopeBatchRow.amounts already held
the correct data throughout). Python cannot execute that JS, so this file
cannot re-run the actual browser behaviour — that was verified directly
against the rendered page with a jsdom harness during development, including
a side-by-side run against the pre-fix template that reproduced the bug
exactly (computed total 0 against a saved total of 200, and an autosave
payload with the fund's amount missing entirely) and confirmed the patched
version resolves it.

What this DOES check, cheaply and permanently: that the two things the JS fix
actually relies on are both present in what the server sends the browser —
because if either regressed, the JS fix would have nothing to work with,
however carefully it were written.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from envelopes.models import EnvelopeBatch, EnvelopeBatchRow


class NonPreferredFundColumnDataIntegrityTests(TestCase):
    """A batch entered against a fund outside PREFERRED must never lose data
    when its ledger page is (re)opened."""

    def setUp(self):
        self.user = User.objects.create_user("ledgertester", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.user)
        # A fund of our own, deliberately named so it can never collide with
        # envelopes.services.posting.PREFERRED.
        self.fund = Department.objects.create(
            name="Not A Preferred Fund", slug="not-a-preferred-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY, active=True)

        self.batch = EnvelopeBatch.objects.create(
            sabbath_date=dt.date(2026, 7, 4), source=EnvelopeBatch.Source.MANUAL,
            status=EnvelopeBatch.Status.DRAFT, created_by=self.user)
        self.row = EnvelopeBatchRow.objects.create(
            batch=self.batch, line_no=1, receipt_no="9001",
            contributor_name="A GIVER", channel="CASH",
            amounts={str(self.fund.id): "200"}, manual_total=Decimal("200"))

    def test_the_rendered_page_offers_a_checkbox_for_the_used_fund(self):
        """The prerequisite the whole fix depends on: without this checkbox
        existing in the DOM at all, no amount of client-side JS could ever
        show or preserve the column."""
        html = self.client.get(
            reverse("envelope_ledger_edit", args=[self.batch.pk])).content.decode()
        self.assertIn(f'value="{self.fund.id}"', html)

    def test_the_rows_amounts_are_sent_to_the_browser_intact(self):
        """The server-side half of the guarantee: EnvelopeBatchRow.amounts is
        never touched by a simple page load, whatever the browser goes on to
        render from it."""
        html = self.client.get(
            reverse("envelope_ledger_edit", args=[self.batch.pk])).content.decode()
        self.assertIn(f'"{self.fund.id}": "200"', html.replace(" ", "").replace(
            f'"{self.fund.id}":"200"', f'"{self.fund.id}": "200"'))
        self.row.refresh_from_db()
        self.assertEqual(self.row.amounts, {str(self.fund.id): "200"})

    def test_the_column_auto_select_fix_is_present_in_the_shipped_script(self):
        """A cheap tripwire: Python can't run the browser logic, but it can
        make sure the specific functions the fix depends on are not silently
        deleted by a future edit to this template. If this test starts
        failing, re-read docs/recommendations.md and the comments around
        usedFundKeysFromRows / currentAmounts in ledger.html before removing
        it — that removal is exactly how this bug came back once already."""
        html = self.client.get(
            reverse("envelope_ledger_edit", args=[self.batch.pk])).content.decode()
        self.assertIn("function usedFundKeysFromRows", html)
        self.assertIn("function currentAmounts", html)
        self.assertIn("tr._amounts", html)
