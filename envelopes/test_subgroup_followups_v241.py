"""Tests for two follow-up fixes to the subgroup work (v2.41):

1. Development Groups regressed because the ledger identified "the"
   Development fund via an ambiguous, unordered `.filter(...).first()` query
   — fragile even before the subgroup work, but the subgroup mechanism's
   render-order check (checking subgroups before the single hardcoded key)
   made the fragility visible. Fixed by generalising to the SAME per-
   department `category == DEVELOPMENT` check the cash-entry form and review
   queue already use (core/views.py, giving/views.py) — every Development
   fund gets its own independent group picker, deterministically, with no
   "first()" involved. Development's own posting behaviour (dev_group is a
   tag, never a re-targeted key) is completely unchanged.

2. Numbered sub-accounts (e.g. "Small Group 7") now correctly post to their
   own account (that was the whole point of the subgroup work), but that
   made the Sabbath statement / monthly summary / Sabbath Excel export
   explode into one column per subgroup for a fund with many of them.
   Fixed by rolling NUMBERED subgroups up to their parent fund for display
   in exactly those three views — established, individually-NAMED sub-
   accounts (Tithe, Camp Meeting under Trust Fund) are deliberately left
   alone, exactly as they've always displayed, since only a fund with many
   numbered children was ever cluttered.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department, numbered_subgroup_parent_map
from envelopes.models import Envelope, EnvelopeLine
from envelopes.services.posting import column_catalog


def _assistant(username="dg_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


SAB = dt.date(2026, 6, 6)


class DevelopmentGroupRobustnessTests(TestCase):
    """Item 1: every Development-category fund gets its own picker,
    deterministically, regardless of how many exist or their id ordering."""

    def setUp(self):
        Department.objects.filter(name__startswith="DGR").delete()
        self.u = _assistant()

    def test_single_development_fund_flagged(self):
        d = Department.objects.create(name="DGR Development", fund_type="LOCAL",
                                      category="DEVELOPMENT")
        cols = {c["key"]: c for c in column_catalog()}
        self.assertTrue(cols[str(d.id)]["is_development"])

    def test_multiple_development_funds_each_flagged_independently(self):
        d1 = Department.objects.create(name="DGR Project A", fund_type="LOCAL",
                                       category="DEVELOPMENT")
        d2 = Department.objects.create(name="DGR Project B", fund_type="LOCAL",
                                       category="DEVELOPMENT")
        d3 = Department.objects.create(name="DGR Ordinary Fund", fund_type="LOCAL",
                                       category="OFFERING")
        cols = {c["key"]: c for c in column_catalog()}
        self.assertTrue(cols[str(d1.id)]["is_development"])
        self.assertTrue(cols[str(d2.id)]["is_development"])
        self.assertFalse(cols[str(d3.id)]["is_development"])

    def test_development_fund_never_gets_generic_subgroups(self):
        # even if a Development fund happens to have real Department.parent
        # children (for whatever reason), it must never show the GENERIC
        # subgroup picker — only its own established DevelopmentGroup tag
        parent = Department.objects.create(name="DGR Dev With Kids",
                                           fund_type="LOCAL", category="DEVELOPMENT")
        Department.objects.create(name="DGR Dev Kid 1", fund_type="LOCAL",
                                  category="DEVELOPMENT", parent=parent)
        cols = {c["key"]: c for c in column_catalog()}
        self.assertEqual(cols[str(parent.id)]["subgroups"], [])

    def test_ledger_page_marks_every_development_column(self):
        Department.objects.create(name="DGR Project A", fund_type="LOCAL",
                                  category="DEVELOPMENT")
        Department.objects.create(name="DGR Project B", fund_type="LOCAL",
                                  category="DEVELOPMENT")
        self.client.force_login(self.u)
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertGreaterEqual(html.count("isDevelopment:true"), 2)
        self.assertNotIn("DEV_FUND_KEY", html)


class NumberedSubgroupRollupTests(TestCase):
    """Item 2: numbered subgroups roll up to their parent in summary views;
    established named sub-accounts are unaffected."""

    def setUp(self):
        Department.objects.filter(name__startswith="NSR").delete()
        self.u = User.objects.create_user("nsr_u", password="x", is_superuser=True)
        self.parent = Department.objects.create(name="NSR Small Groups",
                                                 fund_type="LOCAL")
        self.g1 = Department.objects.create(name="NSR Small Group 1",
                                            fund_type="LOCAL", parent=self.parent)
        self.g2 = Department.objects.create(name="NSR Small Group 2",
                                            fund_type="LOCAL", parent=self.parent)
        self.trust = Department.objects.create(name="NSR Trust Fund", fund_type="TRUST")
        self.tithe = Department.objects.create(name="NSR Tithe", fund_type="TRUST",
                                               parent=self.trust)
        for i, (dept, amt) in enumerate(
                [(self.g1, "100"), (self.g2, "150"), (self.tithe, "200")], start=1):
            env = Envelope.objects.create(date=SAB, receipt_no=f"NSR{i}",
                                          contributor_name=f"P{i}", recorded_by=self.u)
            EnvelopeLine.objects.create(envelope=env, department=dept,
                                        amount=Decimal(amt))
            env.recompute_total(); env.save(update_fields=["total"])

    def test_numbered_map_includes_numbered_excludes_named(self):
        m = numbered_subgroup_parent_map()
        self.assertIn(self.g1.id, m)
        self.assertIn(self.g2.id, m)
        self.assertNotIn(self.tithe.id, m)
        self.assertEqual(m[self.g1.id].id, self.parent.id)

    def test_sabbath_statement_rolls_up_numbered_only(self):
        from reports.services.envelope_reports import sabbath_statement
        stmt = sabbath_statement(SAB)
        names = [f.name for f in stmt["funds"]]
        self.assertIn("NSR Small Groups", names)
        self.assertNotIn("NSR Small Group 1", names)
        self.assertNotIn("NSR Small Group 2", names)
        self.assertIn("NSR Tithe", names)   # named sub-account untouched
        self.assertEqual(stmt["fund_totals"][self.parent.id], Decimal("250"))
        self.assertEqual(stmt["fund_totals"][self.tithe.id], Decimal("200"))

    def test_monthly_summary_rolls_up_numbered_only(self):
        from reports.services.envelope_reports import monthly_summary
        summ = monthly_summary(SAB.year, SAB.month)
        names = [r["fund"].name for r in summ["local_rows"] + summ["trust_rows"]]
        self.assertIn("NSR Small Groups", names)
        self.assertNotIn("NSR Small Group 1", names)
        self.assertIn("NSR Tithe", names)
        parent_row = next(r for r in summ["local_rows"]
                          if r["fund"].id == self.parent.id)
        self.assertEqual(parent_row["total"], Decimal("250"))

    def test_ledger_posting_still_targets_the_exact_subgroup(self):
        # the rollup is display-only — the actual EnvelopeLine/Transaction
        # rows must still be posted to the precise subgroup account
        self.assertTrue(EnvelopeLine.objects.filter(department=self.g1).exists())
        self.assertTrue(EnvelopeLine.objects.filter(department=self.g2).exists())
        self.assertFalse(EnvelopeLine.objects.filter(department=self.parent).exists())

    def test_sabbath_excel_export_rolls_up_numbered_columns(self):
        tr = User.objects.create_user("nsr_tr", password="x")
        tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        c = self.client
        c.force_login(tr)
        r = c.get(f"/envelopes/sabbath.xlsx?date={SAB.isoformat()}")
        self.assertEqual(r.status_code, 200)
        import io, openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        ws = wb.active
        header = [row for row in ws.iter_rows(min_row=1, max_row=5, values_only=True)
                 if row and row[0] == "No"][0]
        self.assertIn("NSR Small Groups", header)
        self.assertNotIn("NSR Small Group 1", header)
        self.assertIn("NSR Tithe", header)

        # find the TOTAL row and confirm the rolled-up figure
        rows = list(ws.iter_rows(values_only=True))
        total_row = next(r for r in rows if r[1] == "TOTAL")
        col_idx = header.index("NSR Small Groups")
        self.assertEqual(total_row[col_idx], 250)
