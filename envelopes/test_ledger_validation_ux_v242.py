"""Tests for the envelope ledger's validation UX fix (v2.42):

The "Envelope total X doesn't match allocation Y" message previously fired on
every keystroke and rendered as floating text inside a narrow table cell
(colliding with neighbouring columns). It now:
  - only evaluates once the cashier has finished a row (moved focus away
    from it — a "focusout" listener on the row, not "input" on each field);
  - renders in one consolidated panel below the table, not inside cells.

These are primarily client-side (JavaScript) changes, verified via a DOM
harness (Node + jsdom) at implementation time — no browser is available in
this sandbox. This file covers the server-rendered page structure: the new
elements exist, the old ones are gone, and nothing about the batch/posting
backend changed (the client-side validation is a UX affordance; the server
remains the authoritative gate at Submit/Approve/Post, unaffected by this
front-end-only change)."""
import datetime as dt

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT
from departments.models import Department
from envelopes.services import batches as bsvc


def _assistant(username="ux_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


class LedgerValidationUiTests(TestCase):
    def setUp(self):
        self.u = _assistant()
        self.client.force_login(self.u)

    def test_row_errors_panel_present_not_old_blocking_help(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertIn('id="rowErrorsPanel"', html)
        self.assertNotIn('id="blockingHelp"', html)

    def test_panel_positioned_after_the_table(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        table_end = html.index('</table>')
        panel_pos = html.index('id="rowErrorsPanel"')
        summary_pos = html.index('id="entrySummary"')
        self.assertGreater(panel_pos, table_end)
        self.assertLess(panel_pos, summary_pos)

    def test_no_in_cell_error_class_shipped(self):
        # .row-err (the old floating in-cell error element/class) must be
        # fully retired, not just unused
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertNotIn(".row-err{", html)
        self.assertNotIn('class="row-err"', html)

    def test_allocated_column_purpose_explained(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertIn("running sum", html.lower())

    def test_focusout_listener_present_not_per_keystroke_validation(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertIn('"focusout"', html)
        # the .amt input's own listener must no longer call validateRow directly
        self.assertIn('i.addEventListener("input",()=>{ rowTotal(tr); markDirtyAndSave(); })', html)

    def test_backend_validation_unaffected_by_frontend_change(self):
        # the server remains authoritative regardless of any client-side UX —
        # a mismatched row still can't be submitted
        fund = Department.objects.create(name="UX Fund", fund_type="LOCAL")
        sab = dt.date(2026, 6, 6)
        batch, _ = bsvc.get_or_create_draft(self.u, None, sab)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "UX1", "contributor_name": "Jane",
             "manual_total": "999", "amounts": {str(fund.id): "100"}}])
        problems = bsvc.submit_batch(batch, self.u)
        self.assertTrue(problems)
        self.assertIn("doesn't match", problems[0])
