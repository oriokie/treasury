"""Posting an envelope batch has to end on the page that holds the receipts
it just created.

`EnvelopeBatchPostView` used to finish with
``redirect("/envelopes/?date=<sabbath>")``. `EnvelopeListView` read only
``?month=YYYY-MM``; ``date`` was not a parameter it had ever known about, so
it was dropped on the floor and the page fell back to TODAY's calendar month.
A treasurer posting a sheet for any Sabbath outside the current month was
therefore shown "Posted 5 envelope(s) for 11 Jul 2026" over an August summary
containing none of them, with no indication that the link had been ignored —
the fifth instance of the shipped-but-unusable shape the e2e suite was written
to catch (docs/recommendations.md #121, #122, #125, #126, #130).

Both halves are pinned here:

* Post now lands on ``envelope_sabbath_entries`` for the Sabbath — the page
  that actually lists the receipts, with the per-receipt reprint/correct
  actions a treasurer reaches for straight after posting. The month overview
  is by design a wall of summary cards and shows no receipt numbers at all,
  so landing there was never going to show anyone their own work.
* ``?date=`` is no longer silently ignored by `EnvelopeListView`, because six
  other redirects in envelopes/views.py still come back that way after
  deleting, reversing or re-sending a Sabbath's receipts, and every one of
  them was quietly landing on the current month too. A month it genuinely
  cannot show now says so instead of substituting a different one in silence
  — silence is what kept this defect invisible.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from envelopes.models import Envelope, EnvelopeBatch
from envelopes.services import batches as bsvc

#: A Saturday, and deliberately a fixed one in the past: the whole point of
#: the defect is a Sabbath that is NOT in the month the calendar is showing,
#: so a date that could drift into "today's month" would stop testing it.
SAB = dt.date(2026, 6, 6)


class _PostedBatch(TestCase):
    """One fund, one approved single-row batch, and a treasurer to post it."""

    def setUp(self):
        self.assistant = User.objects.create_user("pl_asst", password="x")
        self.assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.treasurer = User.objects.create_user("pl_tr", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.tithe = Department.objects.create(name="PlTithe", fund_type="TRUST")
        self.client.force_login(self.treasurer)

    def _approved(self, sabbath_date, receipt="PL-1", name="Grace Wanjiru"):
        batch, _ = bsvc.get_or_create_draft(self.assistant, None, sabbath_date)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": receipt, "contributor_name": name,
             "channel": "CASH", "manual_total": "500",
             "amounts": {str(self.tithe.id): "500"}},
        ])
        self.assertFalse(bsvc.submit_batch(batch, self.assistant))
        self.assertFalse(bsvc.approve_batch(batch, self.treasurer))
        return batch

    def _post(self, batch):
        return self.client.post(
            reverse("envelope_batch_post", args=[batch.pk]), {})


class PostRedirectTests(_PostedBatch):

    def test_posting_lands_on_the_sabbaths_own_entries_page(self):
        batch = self._approved(SAB)
        r = self._post(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.POSTED)
        self.assertRedirects(
            r, reverse("envelope_sabbath_entries", args=[SAB.isoformat()]))

    def test_the_page_it_lands_on_lists_the_receipts_just_posted(self):
        """The assertion that would have caught the original defect: not that
        the redirect went somewhere, but that the somewhere contains the
        treasurer's own work."""
        batch = self._approved(SAB, receipt="PL-EVIDENCE")
        landing = self.client.post(
            reverse("envelope_batch_post", args=[batch.pk]), {}, follow=True)
        self.assertEqual(landing.status_code, 200)
        self.assertContains(landing, "PL-EVIDENCE")
        self.assertEqual(landing.context["sabbath"], SAB)
        self.assertEqual(landing.context["sec"]["total"], Decimal("500"))

    def test_a_midweek_batch_lands_on_the_sabbath_it_is_counted_under(self):
        """A batch may carry a weekday date — the entry grid accepts any date
        and `_save_envelope` files the gift under `sabbath_of(date)`. Redirect
        to the raw date and the treasurer gets a page that buckets by Sabbath
        and therefore holds nothing at all."""
        wednesday = dt.date(2026, 6, 10)
        self.assertEqual(wednesday.weekday(), 2)
        batch = self._approved(wednesday, receipt="PL-MIDWEEK")
        landing = self.client.post(
            reverse("envelope_batch_post", args=[batch.pk]), {}, follow=True)
        self.assertRedirects(
            landing,
            reverse("envelope_sabbath_entries", args=["2026-06-13"]))
        self.assertContains(landing, "PL-MIDWEEK")

    def test_a_refused_post_still_goes_back_to_the_batch(self):
        """Guard against fixing the happy path by redirecting unconditionally:
        a batch that is not approved must still be returned to its own detail
        page with the refusal, not sent to a Sabbath page as if it had
        worked."""
        batch = self._approved(SAB)
        batch.status = EnvelopeBatch.Status.DRAFT
        batch.save(update_fields=["status"])
        r = self._post(batch)
        self.assertRedirects(
            r, reverse("envelope_batch_detail", args=[batch.pk]))
        self.assertEqual(Envelope.objects.count(), 0)


class ListViewDateParamTests(TestCase):
    """`EnvelopeListView` and the ``?date=`` it used to throw away."""

    def setUp(self):
        self.treasurer = User.objects.create_user("pl_list", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

    def _get(self, query=""):
        return self.client.get(reverse("envelope_list") + query)

    def _messages(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_a_date_selects_that_dates_month(self):
        r = self._get("?date=2026-06-06")
        self.assertEqual(r.context["month_value"], "2026-06")
        self.assertEqual(self._messages(r), [])

    def test_a_sunday_selects_the_month_its_giving_is_counted_in(self):
        """28 June 2026 is a Sunday; this page files its envelopes under
        Saturday 4 July, so ?date= must open July — landing on June would
        show a month that, by this page's own bucketing, holds none of that
        day's receipts."""
        r = self._get("?date=2026-06-28")
        self.assertEqual(r.context["month_value"], "2026-07")

    def test_month_still_wins_when_both_are_given(self):
        r = self._get("?month=2026-05&date=2026-06-06")
        self.assertEqual(r.context["month_value"], "2026-05")

    def test_a_bare_visit_is_still_this_month_and_says_nothing(self):
        today = dt.date.today()
        r = self._get()
        self.assertEqual(r.context["month_value"], f"{today:%Y-%m}")
        self.assertEqual(self._messages(r), [])

    def test_an_impossible_month_is_reported_not_swallowed(self):
        """2026-13 parsed as two integers perfectly well, so the old code
        accepted it and then died in dt.date(2026, 13, 1) — a 500 on a
        bookmarked URL. It must now come back as a page that says which month
        it is showing."""
        today = dt.date.today()
        r = self._get("?month=2026-13")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["month_value"], f"{today:%Y-%m}")
        self.assertIn("2026-13", " ".join(self._messages(r)))

    def test_an_unreadable_date_is_reported_not_swallowed(self):
        today = dt.date.today()
        r = self._get("?date=not-a-date")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["month_value"], f"{today:%Y-%m}")
        self.assertIn("not-a-date", " ".join(self._messages(r)))


class OtherDateRedirectsLandCorrectlyTests(TestCase):
    """The rest of the ``?date=`` callers in envelopes/views.py. They were
    wrong in exactly the same way and are fixed by the same half of the fix;
    without this they would go on landing on today's month unnoticed."""

    def setUp(self):
        self.treasurer = User.objects.create_user("pl_other", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)
        self.env = Envelope.objects.create(
            date=SAB, receipt_no="PL-DEL", contributor_name="Peter Otieno",
            total=Decimal("100"), recorded_by=self.treasurer)

    def test_deleting_an_envelope_returns_to_its_own_month(self):
        landing = self.client.post(
            reverse("envelope_delete", args=[self.env.pk]), {}, follow=True)
        self.assertEqual(landing.status_code, 200)
        self.assertEqual(landing.context["month_value"], "2026-06")
