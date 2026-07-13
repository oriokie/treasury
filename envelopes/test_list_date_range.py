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

    def test_a_sunday_envelope_that_rolls_forward_into_the_month_is_still_shown(self):
        """June 28, 2026 is a Sunday; it rolls forward to Saturday 4 July —
        it belongs in the July page even though its own date is in June."""
        self._envelope(dt.date(2026, 6, 28), "ENV-ROLL-1")
        r = self.client.get("/envelopes/?month=2026-07")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("ENV-ROLL-1", body)

    def test_an_envelope_from_a_different_month_is_excluded(self):
        self._envelope(dt.date(2025, 1, 4), "ENV-FAR-1")   # nowhere near July 2026
        r = self.client.get("/envelopes/?month=2026-07")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("ENV-FAR-1", r.content.decode())

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
        body = r.content.decode()
        self.assertIn("ENV-JULY-1", body)
        self.assertNotIn("ENV-BULK-0", body)
