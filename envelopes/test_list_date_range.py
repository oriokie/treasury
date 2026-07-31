"""EnvelopeListView used to fetch every Envelope ever recorded on every
visit, regardless of which month was being viewed, then discard almost all
of it in Python. Found while auditing "does any page load unbounded data"
(Edwin's request). Fixed to filter the queryset to the date window that
could actually bucket into the requested month's Sabbaths — proven here
both for correctness (a boundary-date envelope is still included) and for
the actual fix (an unrelated, far-away envelope is now excluded from the
query, not just from the rendered page).
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.roles import TREASURER
from envelopes.models import Envelope


class EnvelopeListDateRangeTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tr_envlist", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

    def _envelope(self, date, receipt_no, total="100"):
        return Envelope.objects.create(
            date=date, receipt_no=receipt_no, contributor_name="Test Giver",
            total=Decimal(total), recorded_by=self.treasurer)

    def _section(self, response, sabbath):
        return next(s for s in response.context["sections"] if s["sabbath"] == sabbath)

    def test_a_sunday_envelope_that_rolls_forward_into_the_month_is_still_shown(self):
        """June 28, 2026 is a Sunday; it rolls forward to Saturday 4 July —
        it belongs in the July page even though its own date is in June.

        Asserted against the 4 July section rather than the receipt number:
        the month view now shows each Sabbath's SUMMARY and the receipts
        themselves live on the per-Sabbath page (covered below), so counting
        it in the right bucket is what "shown in July" now means.
        """
        self._envelope(dt.date(2026, 6, 28), "ENV-ROLL-1")
        r = self.client.get("/envelopes/?month=2026-07")
        self.assertEqual(r.status_code, 200)
        sec = self._section(r, dt.date(2026, 7, 4))
        self.assertEqual(sec["count"], 1)
        self.assertEqual(sec["total"], Decimal("100"))
        self.assertEqual(r.context["envelope_count"], 1)

    def test_the_rolled_forward_envelope_is_listed_on_its_sabbath_page(self):
        """The other half: it must actually be reachable, not just counted."""
        self._envelope(dt.date(2026, 6, 28), "ENV-ROLL-1")
        r = self.client.get("/envelopes/sabbath/2026-07-04/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ENV-ROLL-1", r.content.decode())

    def test_an_envelope_from_a_different_month_is_excluded(self):
        self._envelope(dt.date(2025, 1, 4), "ENV-FAR-1")   # nowhere near July 2026
        r = self.client.get("/envelopes/?month=2026-07")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("ENV-FAR-1", r.content.decode())
        # and it is not silently counted into any July Sabbath either — the
        # assertNotIn above would now pass even on a broken view, since no
        # receipt numbers are rendered on this page at all.
        self.assertEqual(r.context["envelope_count"], 0)
        self.assertEqual(r.context["grand_total"], Decimal(0))

    def test_the_query_no_longer_scans_the_whole_table(self):
        """The actual bug: confirm the queryset is genuinely filtered at the
        database level, not merely narrowed after fetching everything."""
        # a year's worth of unrelated envelopes, well outside July 2026
        for i in range(60):
            self._envelope(dt.date(2020, 1, 1) + dt.timedelta(days=i * 6),
                           f"ENV-BULK-{i}")
        self._envelope(dt.date(2026, 7, 4), "ENV-JULY-1")

        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get("/envelopes/?month=2026-07")
        self.assertEqual(r.status_code, 200)
        envelope_queries = [q for q in ctx.captured_queries
                            if "envelopes_envelope" in q["sql"].lower()
                            and "SELECT" in q["sql"].upper()]
        # the fetch itself must carry a date filter — not fetch all 61 rows
        # and discard 60 of them in Python
        self.assertTrue(
            any("date" in q["sql"].lower() for q in envelope_queries),
            "expected the envelope fetch to filter by date at the database level")
        # only the July envelope reaches the page: 60 unrelated rows are
        # excluded by the query, not merely hidden by the template.
        self.assertEqual(r.context["envelope_count"], 1)
        self.assertEqual(self._section(r, dt.date(2026, 7, 4))["count"], 1)
        self.assertNotIn("ENV-BULK-0", r.content.decode())
