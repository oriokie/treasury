"""Bulk pledge import, scoped to the leader's own fund.

The parsing is the treasurer's importer. What this adds is the constraint that
matters: the campaign is fixed to one the leader's department owns and
re-checked when the rows are applied, so neither a Campaign column in the
spreadsheet nor a field in the POST can move a pledge onto another fund.
"""
import datetime as dt
import io
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from departments.models import Department, DepartmentLeadership
from members.models import Member
from pledges.models import Pledge, PledgeCampaign


def _workbook(rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Member name", "Phone", "Campaign", "Amount", "Frequency",
               "Start date", "End date", "Note"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = "pledges.xlsx"
    return buf


class _Leader(TestCase):
    def setUp(self):
        from core.roles import LEADER
        self.user = User.objects.create_user("imp", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=LEADER)[0])
        self.dept = Department.objects.create(name="Youth", fund_type="LOCAL")
        DepartmentLeadership.objects.create(user=self.user, department=self.dept)
        self.mine = PledgeCampaign.objects.create(
            name="Youth Camp", target_department=self.dept,
            status=PledgeCampaign.Status.ACTIVE)
        self.other_dept = Department.objects.create(name="Choir",
                                                    fund_type="LOCAL")
        self.theirs = PledgeCampaign.objects.create(
            name="Choir Robes", target_department=self.other_dept,
            status=PledgeCampaign.Status.ACTIVE)
        Member.objects.create(name="ASHA MUTUA", active=True)
        self.client.force_login(self.user)
        self.url = reverse("leader_pledge_import", args=[self.dept.pk])


class AccessTests(_Leader):
    def test_the_page_offers_only_this_fund_s_open_campaigns(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c.pk for c in r.context["campaigns"]], [self.mine.pk])

    def test_a_fund_the_leader_does_not_lead_is_refused(self):
        r = self.client.get(
            reverse("leader_pledge_import", args=[self.other_dept.pk]))
        self.assertEqual(r.status_code, 302)

    def test_the_template_workbook_downloads(self):
        r = self.client.get(self.url, {"download": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])

    def test_no_open_campaign_says_so_instead_of_offering_an_upload(self):
        self.mine.status = PledgeCampaign.Status.CLOSED
        self.mine.save()
        r = self.client.get(self.url)
        self.assertEqual(list(r.context["campaigns"]), [])
        self.assertContains(r, "No campaign for Youth is open")


class ImportTests(_Leader):
    def _upload(self, rows, campaign=None):
        return self.client.post(self.url, {
            "campaign": (campaign or self.mine).pk,
            "file": _workbook(rows)})

    def test_a_file_is_parsed_into_a_review(self):
        r = self._upload([["ASHA MUTUA", "", "", 5000, "One-off",
                           "2026-06-01", "", ""]])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["plan"]), 1)
        self.assertEqual(r.context["plan"][0]["amount"], 5000)

    def test_the_review_points_back_at_the_fund(self):
        r = self._upload([["ASHA MUTUA", "", "", 5000, "One-off",
                           "2026-06-01", "", ""]])
        self.assertEqual(r.context["back_url"],
                         reverse("leader_pledges", args=[self.dept.pk]))

    def test_applying_creates_drafts_on_the_chosen_campaign(self):
        self._upload([["ASHA MUTUA", "", "", 5000, "One-off",
                       "2026-06-01", "", ""]])
        self.client.post(self.url, {"apply": "1", "campaign": self.mine.pk})
        p = Pledge.objects.get()
        self.assertEqual(p.campaign, self.mine)
        self.assertEqual(p.amount, Decimal("5000"))
        self.assertEqual(p.status, Pledge.Status.DRAFT,
                         "a leader gathers promises; the treasurer approves")
        self.assertEqual(p.recorded_by, self.user)

    def test_a_campaign_column_cannot_move_a_pledge_to_another_fund(self):
        """The spreadsheet names the other fund's campaign; it is ignored."""
        self._upload([["ASHA MUTUA", "", "Choir Robes", 5000, "One-off",
                       "2026-06-01", "", ""]])
        self.client.post(self.url, {"apply": "1", "campaign": self.mine.pk})
        self.assertEqual(Pledge.objects.get().campaign, self.mine)

    def test_posting_another_fund_s_campaign_is_refused(self):
        r = self.client.post(self.url, {"campaign": self.theirs.pk,
                                        "file": _workbook([])})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Pledge.objects.exists())

    def test_a_closed_campaign_is_refused(self):
        self.mine.status = PledgeCampaign.Status.CLOSED
        self.mine.save()
        r = self._upload([["ASHA MUTUA", "", "", 5000, "One-off",
                           "2026-06-01", "", ""]])
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Pledge.objects.exists())
