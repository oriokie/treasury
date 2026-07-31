"""The envelopes area was one page doing two jobs: a month of Sabbaths AND
every receipt in that month, with each Sabbath's receipts hidden behind a
<details>. It is now split — /envelopes/ summarises each Sabbath (totals,
trust/local, channel mix, per-fund breakdown) and
/envelopes/sabbath/<YYYY-MM-DD>/ lists that one Sabbath's receipts with the
full set of per-receipt actions.

The routing test below is the one that would otherwise bite silently: the new
route shares its prefix with the pre-existing /envelopes/sabbath/close/, and a
`<str:date>` converter would have swallowed "close" and broken Sabbath closing
depending purely on URL declaration order.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import resolve, reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from envelopes.models import Envelope

SAB = dt.date(2026, 6, 6)


def _user(username, role):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class _Seed(TestCase):
    def setUp(self):
        self.tr = _user("se_tr", TREASURER)
        self.client.force_login(self.tr)
        self.tithe = Department.objects.create(name="SeTithe", fund_type="TRUST")
        self.lcb = Department.objects.create(name="SeLCB", fund_type="LOCAL")

    def _env(self, receipt, name, tithe=0, lcb=0, channel="CASH", date=SAB):
        e = Envelope.objects.create(
            date=date, receipt_no=receipt, contributor_name=name,
            channel=channel, total=Decimal(tithe) + Decimal(lcb),
            recorded_by=self.tr)
        if tithe:
            e.lines.create(department=self.tithe, amount=Decimal(tithe))
        if lcb:
            e.lines.create(department=self.lcb, amount=Decimal(lcb))
        return e


class SabbathEntriesRoutingTests(_Seed):
    def test_the_date_route_and_the_close_route_do_not_collide(self):
        self.assertEqual(resolve("/envelopes/sabbath/close/").url_name, "sabbath_close")
        m = resolve("/envelopes/sabbath/2026-06-06/")
        self.assertEqual(m.url_name, "envelope_sabbath_entries")
        self.assertEqual(m.kwargs["date"], "2026-06-06")

    def test_reverse_builds_the_expected_url(self):
        self.assertEqual(
            reverse("envelope_sabbath_entries", args=["2026-06-06"]),
            "/envelopes/sabbath/2026-06-06/")

    def test_a_non_date_path_segment_never_reaches_this_view(self):
        """Anything that is not YYYY-MM-DD must not resolve here at all —
        that is what keeps 'close' (and any future sibling route) safe."""
        from django.urls.exceptions import Resolver404
        with self.assertRaises(Resolver404):
            resolve("/envelopes/sabbath/not-a-date/")


class SabbathEntriesPageTests(_Seed):
    def setUp(self):
        super().setUp()
        self._env("SE-1", "ALPHA PERSON", tithe=100, lcb=50)
        self._env("SE-2", "BETA PERSON", tithe=200, channel="BANK")

    def test_lists_this_sabbaths_receipts(self):
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "SE-1")
        self.assertContains(r, "ALPHA PERSON")
        self.assertContains(r, "SE-2")

    def test_totals_and_trust_local_split(self):
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        sec = r.context["sec"]
        self.assertEqual(sec["count"], 2)
        self.assertEqual(sec["total"], Decimal("350"))
        self.assertEqual(sec["trust"], Decimal("300"))     # 100 + 200 tithe
        self.assertEqual(sec["local"], Decimal("50"))
        self.assertEqual(sec["cash"], Decimal("150"))      # SE-1 only
        self.assertEqual(sec["bank"], Decimal("200"))

    def test_fund_breakdown_is_trust_first(self):
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        funds = r.context["sec"]["funds"]
        self.assertEqual([f["fund"].name for f in funds], ["SeTithe", "SeLCB"])
        self.assertEqual(funds[0]["amount"], Decimal("300"))
        self.assertEqual(funds[1]["amount"], Decimal("50"))

    def test_a_receipt_from_another_sabbath_is_excluded(self):
        self._env("SE-OTHER", "GAMMA PERSON", tithe=999,
                  date=SAB + dt.timedelta(days=7))
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        self.assertNotContains(r, "SE-OTHER")
        self.assertEqual(r.context["sec"]["total"], Decimal("350"))

    def test_midweek_giving_rolls_into_the_coming_sabbath(self):
        """A Sunday-Friday envelope belongs to the Sabbath ahead of it — the
        same bucketing the month view uses."""
        self._env("SE-WED", "DELTA PERSON", lcb=25, date=SAB - dt.timedelta(days=3))
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        self.assertContains(r, "SE-WED")
        self.assertEqual(r.context["sec"]["total"], Decimal("375"))

    def test_carries_the_per_receipt_actions(self):
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        e = Envelope.objects.get(receipt_no="SE-1")
        for url in (reverse("envelope_detail", args=[e.id]),
                    reverse("envelope_edit", args=[e.id]),
                    reverse("envelope_receipt", args=[e.id]),
                    reverse("envelope_delete", args=[e.id]),
                    reverse("envelope_reassign", args=[e.id])):
            self.assertContains(r, url)

    def test_carries_the_whole_sabbath_actions(self):
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        self.assertContains(r, reverse("envelope_receipts_bulk"))
        self.assertContains(r, reverse("envelope_sabbath_excel"))
        self.assertContains(r, reverse("sabbath_close"))

    def test_empty_sabbath_renders_without_the_table(self):
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-13"]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["sec"]["count"], 0)
        self.assertNotContains(r, "SE-1")

    def test_a_malformed_date_redirects_rather_than_500s(self):
        r = self.client.get("/envelopes/sabbath/2026-13-99/")
        self.assertEqual(r.status_code, 302)
        self.assertRedirects(r, reverse("envelope_list"))

    def test_an_auditor_may_read_it(self):
        from core.roles import AUDITOR
        self.client.force_login(_user("se_aud", AUDITOR))
        r = self.client.get(reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "SE-1")


class MonthOverviewSummaryTests(_Seed):
    def setUp(self):
        super().setUp()
        self._env("MO-1", "ALPHA PERSON", tithe=100, lcb=50)
        self._env("MO-2", "BETA PERSON", tithe=200, channel="BANK")

    def test_month_view_summarises_and_links_but_does_not_list_receipts(self):
        r = self.client.get(reverse("envelope_list") + "?month=2026-06")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, reverse("envelope_sabbath_entries", args=["2026-06-06"]))
        self.assertContains(r, "SeTithe")           # fund breakdown is shown
        self.assertNotContains(r, "MO-1")           # receipts are not
        self.assertNotContains(r, "ALPHA PERSON")

    def test_month_totals(self):
        r = self.client.get(reverse("envelope_list") + "?month=2026-06")
        self.assertEqual(r.context["grand_total"], Decimal("350"))
        self.assertEqual(r.context["month_trust"], Decimal("300"))
        self.assertEqual(r.context["month_local"], Decimal("50"))
        self.assertEqual(r.context["envelope_count"], 2)
        self.assertEqual(r.context["active_sabbaths"], 1)

    def test_month_fund_rollup_matches_the_sabbath_breakdown(self):
        r = self.client.get(reverse("envelope_list") + "?month=2026-06")
        month = {f["fund"].name: f["amount"] for f in r.context["month_funds"]}
        self.assertEqual(month, {"SeTithe": Decimal("300"), "SeLCB": Decimal("50")})

    def test_every_saturday_of_the_month_gets_a_card(self):
        r = self.client.get(reverse("envelope_list") + "?month=2026-06")
        self.assertEqual([s["sabbath"] for s in r.context["sections"]],
                         [dt.date(2026, 6, d) for d in (6, 13, 20, 27)])
