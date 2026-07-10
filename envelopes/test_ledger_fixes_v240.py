"""Tests for the envelope-ledger production fixes (v2.40):

1. /envelopes/ledger/<pk>/ 500'd — EnvelopeLedgerCreate.get() didn't accept the
   URL's pk kwarg. Fixed and covered here, including the stale/foreign-batch
   redirect path.
2. Deleting an uncommitted draft had a working backend endpoint but no UI
   button anywhere — added to both the batch list and detail pages.
3/4. Dashboard chart sizing + PNG-vs-JPEG exports are covered by their own
   template/service changes; not re-tested at the unit level here (no new
   Python logic — see the chart-image and goal_chart test suites).
5. Selecting Bank/Cash (or a Development Group) on one row didn't propagate
   to later rows — only a new row's *creation* copied the row above. Fixed
   with a cascade mirroring the receipt-number cascade; the cascade itself is
   pure client-side logic, verified via the DOM harness noted in the code
   comments (no server-side surface to unit test).
6. Generalised the Development-Group-style subgroup picker to any fund with
   real Department.parent children (e.g. Trust Fund -> Tithe, Camp Meeting),
   and the Excel import's "Group Number" column now also re-attributes a
   matching numbered subgroup for those funds, reusing the identical
   number-matching idea a numbered fund family already uses for narrations.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from envelopes.models import EnvelopeBatch
from envelopes.services import batches as bsvc
from envelopes.services.posting import (_trailing_number, column_catalog,
                                        rekey_to_subgroups, subgroups_for)


def _assistant(username="fx_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


def _treasurer(username="fx_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


SAB = dt.date(2026, 6, 6)


class LedgerEditUrlTests(TestCase):
    """Item 1: /envelopes/ledger/<pk>/ crashed with
    TypeError: EnvelopeLedgerCreate.get() got an unexpected keyword argument 'pk'."""

    def setUp(self):
        self.u = _assistant()
        self.tithe = Department.objects.create(name="Fx1Tithe", fund_type="TRUST")
        self.client.force_login(self.u)

    def test_edit_own_draft_by_pk_no_longer_crashes(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "FX1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        r = self.client.get(reverse("envelope_ledger_edit", args=[batch.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"FX1", r.content)

    def test_stale_or_foreign_pk_redirects_cleanly(self):
        r = self.client.get(reverse("envelope_ledger_edit", args=[999999]))
        self.assertEqual(r.status_code, 302)
        self.assertRedirects(r, reverse("envelope_batch_list"))

    def test_someone_elses_draft_by_pk_does_not_leak(self):
        other = _assistant("fx_asst2")
        batch, _ = bsvc.get_or_create_draft(other, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "PRIV9", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        r = self.client.get(reverse("envelope_ledger_edit", args=[batch.pk]))
        self.assertEqual(r.status_code, 302)

    def test_submitted_batchs_edit_link_redirects_not_crashes(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "FX2", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        bsvc.submit_batch(batch, self.u)
        r = self.client.get(reverse("envelope_ledger_edit", args=[batch.pk]))
        self.assertEqual(r.status_code, 302)


class DeleteDraftUiTests(TestCase):
    """Item 2: the delete-draft endpoint existed but no page linked to it."""

    def setUp(self):
        self.u = _assistant()
        self.tithe = Department.objects.create(name="Fx2Tithe", fund_type="TRUST")
        self.client.force_login(self.u)
        self.batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(self.batch, [
            {"line_no": 1, "receipt_no": "FX3", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])

    def test_delete_button_on_list_page(self):
        r = self.client.get(reverse("envelope_batch_list"))
        self.assertContains(r, reverse("envelope_batch_delete", args=[self.batch.pk]))

    def test_delete_button_on_detail_page(self):
        r = self.client.get(reverse("envelope_batch_detail", args=[self.batch.pk]))
        self.assertContains(r, reverse("envelope_batch_delete", args=[self.batch.pk]))
        self.assertContains(r, "Delete draft")

    def test_delete_actually_removes_the_draft(self):
        r = self.client.post(reverse("envelope_batch_delete", args=[self.batch.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(EnvelopeBatch.objects.filter(pk=self.batch.pk).exists())

    def test_cannot_delete_someone_elses_draft(self):
        other = _assistant("fx_asst3")
        self.client.force_login(other)
        r = self.client.post(reverse("envelope_batch_delete", args=[self.batch.pk]))
        self.assertTrue(EnvelopeBatch.objects.filter(pk=self.batch.pk).exists())

    def test_cannot_delete_a_submitted_batch(self):
        bsvc.submit_batch(self.batch, self.u)
        r = self.client.post(reverse("envelope_batch_delete", args=[self.batch.pk]))
        self.assertTrue(EnvelopeBatch.objects.filter(pk=self.batch.pk).exists())


class SubgroupBackendTests(TestCase):
    """Item 6 backend: generic subgroup metadata + numbered-subgroup rekeying."""

    def setUp(self):
        Department.objects.filter(name__startswith="Fx6").delete()
        self.parent = Department.objects.create(name="Fx6 Small Groups", fund_type="LOCAL")
        self.c1 = Department.objects.create(name="Fx6 Small Group 1", fund_type="LOCAL",
                                            parent=self.parent)
        self.c7 = Department.objects.create(name="Fx6 Small Group 7", fund_type="LOCAL",
                                            parent=self.parent)
        self.plain = Department.objects.create(name="Fx6 Plain Fund", fund_type="LOCAL")

    def test_trailing_number_extraction(self):
        self.assertEqual(_trailing_number("Small Group 7"), 7)
        self.assertEqual(_trailing_number("Group_03"), 3)
        self.assertIsNone(_trailing_number("No digits here"))
        self.assertIsNone(_trailing_number(""))

    def test_subgroups_for_returns_numbered_children(self):
        sg = subgroups_for(self.parent)
        self.assertEqual(len(sg), 2)
        self.assertEqual([s["number"] for s in sg], [1, 7])

    def test_subgroups_for_empty_when_no_children(self):
        self.assertEqual(subgroups_for(self.plain), [])

    def test_column_catalog_carries_subgroups(self):
        cols = {c["key"]: c for c in column_catalog()}
        self.assertIn(str(self.parent.id), cols)
        self.assertEqual(len(cols[str(self.parent.id)]["subgroups"]), 2)
        self.assertEqual(cols[str(self.plain.id)]["subgroups"], [])

    def test_rekey_to_matching_number(self):
        funds = {d.id: d for d in Department.objects.all()}
        amounts = {str(self.parent.id): "500"}
        out = rekey_to_subgroups(amounts, 7, funds)
        self.assertEqual(out, {str(self.c7.id): "500"})

    def test_rekey_no_match_stays_on_parent(self):
        funds = {d.id: d for d in Department.objects.all()}
        amounts = {str(self.parent.id): "500"}
        out = rekey_to_subgroups(amounts, 99, funds)
        self.assertEqual(out, {str(self.parent.id): "500"})

    def test_rekey_none_number_is_noop(self):
        funds = {d.id: d for d in Department.objects.all()}
        amounts = {str(self.parent.id): "500", str(self.plain.id): "20"}
        out = rekey_to_subgroups(amounts, None, funds)
        self.assertEqual(out, amounts)

    def test_rekey_leaves_funds_without_subgroups_untouched(self):
        funds = {d.id: d for d in Department.objects.all()}
        amounts = {str(self.plain.id): "20"}
        out = rekey_to_subgroups(amounts, 1, funds)
        self.assertEqual(out, {str(self.plain.id): "20"})


class SubgroupImportTests(TestCase):
    """Item 6, the Excel import path: a 'Group Number' column re-attributes a
    subgroup-capable fund's amount to the matching numbered child, reusing
    the row-wide group cell the same way Development already does."""

    def setUp(self):
        Department.objects.filter(name__startswith="Fx6imp").delete()
        self.tr = _treasurer("fx6_tr")
        self.parent = Department.objects.create(name="Fx6imp Trust Fund", fund_type="TRUST")
        self.g3 = Department.objects.create(name="Fx6imp Group 3", fund_type="TRUST",
                                            parent=self.parent)
        self.client.force_login(self.tr)

    def _xlsx(self, headers, row):
        import io, openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(headers); ws.append(row)
        buf = io.BytesIO(); wb.save(buf)
        return SimpleUploadedFile("g.xlsx", buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_group_number_rekeys_matching_subgroup(self):
        f = self._xlsx(["Contributor name", "Receipt no", self.parent.name, "Group Number"],
                       ["Fay", "GR1", 400, 3])
        self.client.post("/envelopes/import/", {"date": "2026-06-06", "file": f})
        batch = EnvelopeBatch.objects.filter(
            source=EnvelopeBatch.Source.IMPORT).order_by("-id").first()
        self.assertIsNotNone(batch)
        row = batch.rows.get()
        self.assertEqual(list(row.amounts.keys()), [str(self.g3.id)])
        self.assertEqual(Decimal(str(row.amounts[str(self.g3.id)])), Decimal("400"))

    def test_group_number_with_no_match_keeps_parent(self):
        f = self._xlsx(["Contributor name", "Receipt no", self.parent.name, "Group Number"],
                       ["Gus", "GR2", 400, 99])
        self.client.post("/envelopes/import/", {"date": "2026-06-06", "file": f})
        batch = EnvelopeBatch.objects.filter(
            source=EnvelopeBatch.Source.IMPORT).order_by("-id").first()
        row = batch.rows.get()
        self.assertEqual(list(row.amounts.keys()), [str(self.parent.id)])
        self.assertEqual(Decimal(str(row.amounts[str(self.parent.id)])), Decimal("400"))

    def test_still_posts_correctly_to_the_subgroup_fund(self):
        f = self._xlsx(["Contributor name", "Receipt no", self.parent.name, "Group Number"],
                       ["Hana", "GR3", 250, 3])
        self.client.post("/envelopes/import/", {"date": "2026-06-06", "file": f})
        batch = EnvelopeBatch.objects.filter(
            source=EnvelopeBatch.Source.IMPORT).order_by("-id").first()
        self.assertFalse(bsvc.approve_batch(batch, self.tr))
        problems, count = bsvc.post_batch(batch, self.tr)
        self.assertFalse(problems)
        from giving.models import Transaction
        txn = Transaction.objects.get()
        self.assertEqual(txn.department_id, self.g3.id)
        self.assertEqual(txn.amount, Decimal("250"))
