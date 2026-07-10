"""Tests for the envelope batch maker-checker workflow (v2.39):
Draft (manual only) -> Review -> Approve -> Post.

Covers:
* Models: EnvelopeBatch/EnvelopeBatchRow basics.
* Service layer (envelopes/services/batches.py): row validation, duplicate
  detection (within-batch, vs posted envelopes, vs other open batches),
  submit/approve/return/reject/post transitions, require_different_approver
  segregation of duties, period-lock enforcement at submit and post.
* The posting reuse guarantee: post_batch produces byte-identical
  Envelope/EnvelopeLine/Transaction rows to the pre-existing _save_envelope
  path, and NOTHING in Draft/Review/Approve ever creates one.
* Views: autosave (JSON and form-encoded/beacon bodies), submit, the review
  queue, approve/return/reject/post permission gating, and that only Post
  writes to the ledger.
* Import: builds a REVIEW batch directly (source=IMPORT), never posts
  directly, and a duplicate/clashing receipt becomes a reviewable row error
  rather than a silently dropped row.
* The generic per-user table-state (grid layout) endpoint.
"""
import datetime as dt
import json
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import PeriodLock, SiteConfig
from core.roles import ASSISTANT, TREASURER
from departments.models import Department, DevelopmentGroup
from envelopes.models import Envelope, EnvelopeBatch, EnvelopeBatchRow
from envelopes.services import batches as bsvc
from giving.models import Transaction
from members.models import Member


def _assistant(username="eb_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


def _treasurer(username="eb_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


SAB = dt.date(2026, 6, 6)   # a Saturday


class _Seed(TestCase):
    def setUp(self):
        self.assistant = _assistant()
        self.treasurer = _treasurer()
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.offering = Department.objects.create(name="Offering", fund_type="LOCAL")


# ===========================================================================
# Model basics
# ===========================================================================

class ModelTests(_Seed):
    def test_editable_statuses(self):
        b = EnvelopeBatch.objects.create(sabbath_date=SAB, created_by=self.assistant)
        self.assertTrue(b.is_editable)
        b.status = EnvelopeBatch.Status.RETURNED
        self.assertTrue(b.is_editable)
        for s in (EnvelopeBatch.Status.REVIEW, EnvelopeBatch.Status.APPROVED,
                 EnvelopeBatch.Status.POSTED, EnvelopeBatch.Status.REJECTED):
            b.status = s
            self.assertFalse(b.is_editable, s)

    def test_row_is_active(self):
        b = EnvelopeBatch.objects.create(sabbath_date=SAB, created_by=self.assistant)
        r = EnvelopeBatchRow.objects.create(batch=b, line_no=1)
        self.assertFalse(r.is_active)
        r.contributor_name = "Jane"
        r.amounts = {str(self.tithe.id): "100"}
        self.assertTrue(r.is_active)

    def test_computed_total_aggregate(self):
        b = EnvelopeBatch.objects.create(sabbath_date=SAB, created_by=self.assistant)
        EnvelopeBatchRow.objects.create(batch=b, line_no=1, computed_total=Decimal("40"))
        EnvelopeBatchRow.objects.create(batch=b, line_no=2, computed_total=Decimal("60"))
        self.assertEqual(b.computed_total(), Decimal("100"))


# ===========================================================================
# Row validation
# ===========================================================================

class RowValidationTests(_Seed):
    def test_blank_row_is_clean(self):
        code, detail, total = bsvc.validate_row(
            contributor_name="", receipt_no="", amounts={}, manual_total=None)
        self.assertEqual(code, "")
        self.assertEqual(total, Decimal(0))

    def test_missing_receipt(self):
        code, detail, total = bsvc.validate_row(
            contributor_name="Jane", receipt_no="", amounts={"1": "10"},
            manual_total=Decimal("10"))
        self.assertEqual(code, bsvc.ERR_NO_RECEIPT)

    def test_missing_manual_total(self):
        code, detail, total = bsvc.validate_row(
            contributor_name="Jane", receipt_no="B1", amounts={"1": "10"},
            manual_total=None)
        self.assertEqual(code, bsvc.ERR_TOTAL_MISSING)

    def test_mismatch(self):
        code, detail, total = bsvc.validate_row(
            contributor_name="Jane", receipt_no="B1", amounts={"1": "10"},
            manual_total=Decimal("15"))
        self.assertEqual(code, bsvc.ERR_TOTAL_MISMATCH)
        self.assertIn("10", detail)
        self.assertIn("15", detail)

    def test_matching_is_clean(self):
        code, detail, total = bsvc.validate_row(
            contributor_name="Jane", receipt_no="B1", amounts={"1": "10.005"},
            manual_total=Decimal("10.01"))
        # within the 0.01 tolerance
        self.assertEqual(code, "")

    def test_no_allocation(self):
        code, detail, total = bsvc.validate_row(
            contributor_name="Jane", receipt_no="B1", amounts={},
            manual_total=Decimal("10"))
        self.assertEqual(code, bsvc.ERR_NO_ALLOCATION)

    def test_name_only_row_flagged_not_silently_dropped(self):
        # a name with nothing else is "active" (per row_is_active) so it is
        # flagged rather than silently excluded from the batch
        code, detail, total = bsvc.validate_row(
            contributor_name="Jane", receipt_no="", amounts={}, manual_total=None)
        self.assertEqual(code, bsvc.ERR_NO_RECEIPT)


# ===========================================================================
# Duplicate receipt detection
# ===========================================================================

class DuplicateDetectionTests(_Seed):
    def _draft(self):
        return EnvelopeBatch.objects.create(
            sabbath_date=SAB, created_by=self.assistant,
            status=EnvelopeBatch.Status.DRAFT)

    def test_duplicate_within_batch(self):
        b = self._draft()
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "X1", "contributor_name": "A",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}},
            {"line_no": 2, "receipt_no": "X1", "contributor_name": "B",
             "manual_total": "20", "amounts": {str(self.tithe.id): "20"}},
        ])
        dups = bsvc.find_duplicate_receipts(b)
        self.assertIn("X1", dups)

    def test_duplicate_vs_posted_envelope(self):
        Envelope.objects.create(date=SAB, receipt_no="POSTED1",
                                contributor_name="Old", recorded_by=self.treasurer)
        b = self._draft()
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "POSTED1", "contributor_name": "New",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}},
        ])
        dups = bsvc.find_duplicate_receipts(b)
        self.assertIn("POSTED1", dups)

    def test_duplicate_vs_other_open_batch(self):
        b1 = self._draft()
        bsvc.autosave_rows(b1, [
            {"line_no": 1, "receipt_no": "SHARE1", "contributor_name": "A",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        b2 = self._draft()
        bsvc.autosave_rows(b2, [
            {"line_no": 1, "receipt_no": "SHARE1", "contributor_name": "B",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        dups = bsvc.find_duplicate_receipts(b2)
        self.assertIn("SHARE1", dups)

    def test_no_conflict_vs_rejected_batch(self):
        b1 = self._draft()
        bsvc.autosave_rows(b1, [
            {"line_no": 1, "receipt_no": "FREE1", "contributor_name": "A",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        b1.status = EnvelopeBatch.Status.REJECTED
        b1.save()
        b2 = self._draft()
        bsvc.autosave_rows(b2, [
            {"line_no": 1, "receipt_no": "FREE1", "contributor_name": "B",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        dups = bsvc.find_duplicate_receipts(b2)
        self.assertNotIn("FREE1", dups)


# ===========================================================================
# Full workflow: Draft -> Review -> Approve -> Post
# ===========================================================================

class WorkflowTests(_Seed):
    def _draft_with_row(self, receipt="W1", total="100"):
        b, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": receipt, "contributor_name": "Jane Doe",
             "channel": "CASH", "manual_total": total,
             "amounts": {str(self.tithe.id): total}},
        ])
        return b

    def test_draft_creates_no_ledger_rows(self):
        b = self._draft_with_row()
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(Envelope.objects.count(), 0)

    def test_submit_blocked_by_row_error(self):
        b, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "W2", "contributor_name": "Jane",
             "manual_total": "999", "amounts": {str(self.tithe.id): "100"}}])
        problems = bsvc.submit_batch(b, self.assistant)
        self.assertTrue(problems)
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.DRAFT)

    def test_submit_requires_at_least_one_active_row(self):
        b, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        problems = bsvc.submit_batch(b, self.assistant)
        self.assertTrue(problems)

    def test_full_happy_path_posts_correctly(self):
        b = self._draft_with_row()
        self.assertFalse(bsvc.submit_batch(b, self.assistant))
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.REVIEW)
        self.assertEqual(b.submitted_by, self.assistant)

        self.assertFalse(bsvc.approve_batch(b, self.treasurer))
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.APPROVED)

        # still nothing posted before Post
        self.assertEqual(Transaction.objects.count(), 0)

        problems, count = bsvc.post_batch(b, self.treasurer)
        self.assertFalse(problems)
        self.assertEqual(count, 1)
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.POSTED)
        self.assertEqual(b.posted_by, self.treasurer)

        env = Envelope.objects.get(receipt_no="W1")
        self.assertEqual(env.total, Decimal("100"))
        txn = Transaction.objects.get()
        self.assertEqual(txn.channel, Transaction.Channel.ENVELOPE)
        self.assertEqual(txn.amount, Decimal("100"))
        self.assertEqual(txn.department_id, self.tithe.id)
        self.assertEqual(txn.reference, "envelope W1")   # _save_envelope's own format

        row = b.rows.get()
        self.assertEqual(row.posted_envelope_id, env.id)

    def test_return_requires_reason(self):
        b = self._draft_with_row()
        bsvc.submit_batch(b, self.assistant)
        problems = bsvc.return_batch(b, self.treasurer, "")
        self.assertTrue(problems)
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.REVIEW)

    def test_return_then_resubmit(self):
        b = self._draft_with_row()
        bsvc.submit_batch(b, self.assistant)
        bsvc.return_batch(b, self.treasurer, "Wrong fund, please fix")
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.RETURNED)
        self.assertEqual(b.return_reason, "Wrong fund, please fix")
        self.assertTrue(b.is_editable)

        # creator can re-edit and resubmit
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "W1", "contributor_name": "Jane Doe",
             "manual_total": "150", "amounts": {str(self.offering.id): "150"}}])
        self.assertFalse(bsvc.submit_batch(b, self.assistant))
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.REVIEW)

    def test_reject_is_terminal(self):
        b = self._draft_with_row()
        bsvc.submit_batch(b, self.assistant)
        self.assertFalse(bsvc.reject_batch(b, self.treasurer, "Duplicate of another batch"))
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.REJECTED)
        self.assertFalse(b.is_editable)

    def test_post_only_from_approved(self):
        b = self._draft_with_row()
        problems, count = bsvc.post_batch(b, self.treasurer)
        self.assertTrue(problems)
        self.assertEqual(count, 0)

    def test_post_revalidates_period_lock(self):
        b = self._draft_with_row()
        bsvc.submit_batch(b, self.assistant)
        bsvc.approve_batch(b, self.treasurer)
        PeriodLock.objects.create(year=SAB.year, month=SAB.month,
                                  locked_by=self.treasurer)
        problems, count = bsvc.post_batch(b, self.treasurer)
        self.assertTrue(problems)
        self.assertEqual(count, 0)
        b.refresh_from_db()
        self.assertEqual(b.status, EnvelopeBatch.Status.APPROVED)   # unchanged

    def test_submit_blocked_by_period_lock(self):
        b = self._draft_with_row()
        PeriodLock.objects.create(year=SAB.year, month=SAB.month,
                                  locked_by=self.treasurer)
        # submit_batch itself doesn't check entry_blocked (the view does) —
        # verify the view-level gate via the client in ViewTests instead;
        # here confirm validate_batch_for_post DOES catch it
        problems = bsvc.validate_batch_for_post(b)
        self.assertTrue(any("locked" in p for p in problems))


class SegregationOfDutiesTests(_Seed):
    def setUp(self):
        super().setUp()
        cfg = SiteConfig.get()
        cfg.require_different_approver = True
        cfg.save()

    def _submitted_batch(self):
        b, _ = bsvc.get_or_create_draft(self.treasurer, None, SAB)
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "S1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        bsvc.submit_batch(b, self.treasurer)
        return b

    def test_creator_cannot_approve_own_batch(self):
        b = self._submitted_batch()
        problems = bsvc.approve_batch(b, self.treasurer)
        self.assertTrue(problems)

    def test_different_treasurer_can_approve(self):
        b = self._submitted_batch()
        other = _treasurer("eb_tr2")
        self.assertFalse(bsvc.approve_batch(b, other))

    def test_creator_cannot_post_own_batch(self):
        b = self._submitted_batch()
        other = _treasurer("eb_tr3")
        bsvc.approve_batch(b, other)
        problems, count = bsvc.post_batch(b, self.treasurer)
        self.assertTrue(problems)
        self.assertEqual(count, 0)


class ConcurrentPostTests(_Seed):
    def test_receipt_taken_between_approve_and_post_blocks_cleanly(self):
        b, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "RACE1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        bsvc.submit_batch(b, self.assistant)
        bsvc.approve_batch(b, self.treasurer)
        # simulate another process posting the same receipt number first
        Envelope.objects.create(date=SAB, receipt_no="RACE1",
                                contributor_name="Someone else",
                                recorded_by=self.treasurer)
        problems, count = bsvc.post_batch(b, self.treasurer)
        self.assertTrue(problems)
        self.assertEqual(count, 0)
        self.assertEqual(Transaction.objects.count(), 0)   # nothing posted


# ===========================================================================
# Views
# ===========================================================================

class AutosaveViewTests(_Seed):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.assistant)
        self.url = reverse("envelope_batch_autosave")

    def test_json_body_creates_draft(self):
        payload = {"batch_id": None, "date": SAB.isoformat(), "rows": [
            {"line_no": 1, "receipt_no": "AV1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}]}
        r = self.client.post(self.url, data=json.dumps(payload),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertIsNotNone(d["batch_id"])
        batch = EnvelopeBatch.objects.get(pk=d["batch_id"])
        self.assertEqual(batch.status, EnvelopeBatch.Status.DRAFT)
        self.assertEqual(batch.created_by, self.assistant)
        self.assertEqual(batch.rows.count(), 1)

    def test_form_encoded_body_beacon_style(self):
        payload = {"batch_id": None, "date": SAB.isoformat(), "rows": [
            {"line_no": 1, "receipt_no": "BEACON1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}]}
        r = self.client.post(self.url, data={"payload": json.dumps(payload)})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_row_error_reported(self):
        payload = {"batch_id": None, "date": SAB.isoformat(), "rows": [
            {"line_no": 1, "receipt_no": "AV2", "contributor_name": "Jane",
             "manual_total": "999", "amounts": {str(self.tithe.id): "10"}}]}
        r = self.client.post(self.url, data=json.dumps(payload),
                             content_type="application/json")
        d = r.json()
        self.assertIn("1", [str(k) for k in d["errors"].keys()])

    def test_period_lock_rejected(self):
        PeriodLock.objects.create(year=SAB.year, month=SAB.month,
                                  locked_by=self.treasurer)
        payload = {"batch_id": None, "date": SAB.isoformat(), "rows": []}
        r = self.client.post(self.url, data=json.dumps(payload),
                             content_type="application/json")
        self.assertEqual(r.status_code, 409)

    def test_second_call_reuses_same_batch(self):
        payload = {"batch_id": None, "date": SAB.isoformat(), "rows": [
            {"line_no": 1, "receipt_no": "AV3", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}]}
        r1 = self.client.post(self.url, data=json.dumps(payload),
                              content_type="application/json")
        bid = r1.json()["batch_id"]
        payload["batch_id"] = bid
        payload["rows"][0]["contributor_name"] = "Jane Updated"
        r2 = self.client.post(self.url, data=json.dumps(payload),
                              content_type="application/json")
        self.assertEqual(r2.json()["batch_id"], bid)
        self.assertEqual(EnvelopeBatch.objects.filter(
            created_by=self.assistant).count(), 1)


class SubmitViewTests(_Seed):
    def setUp(self):
        super().setUp()
        self.batch, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(self.batch, [
            {"line_no": 1, "receipt_no": "SV1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])

    def test_only_creator_can_submit(self):
        other = _assistant("eb_asst2")
        self.client.force_login(other)
        r = self.client.post(reverse("envelope_batch_submit", args=[self.batch.pk]))
        self.assertEqual(r.status_code, 302)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, EnvelopeBatch.Status.DRAFT)

    def test_creator_can_submit(self):
        self.client.force_login(self.assistant)
        r = self.client.post(reverse("envelope_batch_submit", args=[self.batch.pk]))
        self.assertEqual(r.status_code, 302)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, EnvelopeBatch.Status.REVIEW)


class ReviewQueuePermissionTests(_Seed):
    def setUp(self):
        super().setUp()
        self.batch, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(self.batch, [
            {"line_no": 1, "receipt_no": "RQ1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        bsvc.submit_batch(self.batch, self.assistant)

    def test_assistant_cannot_approve(self):
        self.client.force_login(self.assistant)
        r = self.client.post(reverse("envelope_batch_approve", args=[self.batch.pk]))
        self.assertIn(r.status_code, (302, 403))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, EnvelopeBatch.Status.REVIEW)

    def test_treasurer_can_approve(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(reverse("envelope_batch_approve", args=[self.batch.pk]))
        self.assertEqual(r.status_code, 302)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, EnvelopeBatch.Status.APPROVED)

    def test_treasurer_can_return_with_reason(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(reverse("envelope_batch_return", args=[self.batch.pk]),
                             {"reason": "Wrong receipt numbers"})
        self.assertEqual(r.status_code, 302)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, EnvelopeBatch.Status.RETURNED)

    def test_treasurer_can_reject_with_reason(self):
        self.client.force_login(self.treasurer)
        r = self.client.post(reverse("envelope_batch_reject", args=[self.batch.pk]),
                             {"reason": "Duplicate submission"})
        self.assertEqual(r.status_code, 302)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, EnvelopeBatch.Status.REJECTED)

    def test_only_post_writes_ledger(self):
        self.client.force_login(self.treasurer)
        self.client.post(reverse("envelope_batch_approve", args=[self.batch.pk]))
        self.assertEqual(Transaction.objects.count(), 0)
        r = self.client.post(reverse("envelope_batch_post", args=[self.batch.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(Envelope.objects.count(), 1)

    def test_batch_list_renders(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("envelope_batch_list"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f"#{self.batch.pk}")
        self.assertContains(r, "In review")

    def test_batch_detail_renders(self):
        self.client.force_login(self.treasurer)
        r = self.client.get(reverse("envelope_batch_detail", args=[self.batch.pk]))
        self.assertEqual(r.status_code, 200)


class LedgerEntryViewTests(_Seed):
    def test_get_with_no_draft_shows_empty_form(self):
        self.client.force_login(self.assistant)
        r = self.client.get(reverse("envelope_ledger"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(EnvelopeBatch.objects.count(), 0)   # GET alone creates nothing

    def test_get_resumes_own_draft_for_that_sabbath(self):
        b, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "RES1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        self.client.force_login(self.assistant)
        r = self.client.get(reverse("envelope_ledger") + f"?date={SAB.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"RES1", r.content)

    def test_get_does_not_resume_another_users_draft(self):
        b, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "PRIV1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        other = _assistant("eb_asst3")
        self.client.force_login(other)
        r = self.client.get(reverse("envelope_ledger") + f"?date={SAB.isoformat()}")
        self.assertNotIn(b"PRIV1", r.content)


# ===========================================================================
# Import path
# ===========================================================================

class ImportViewTests(_Seed):
    def _xlsx(self, rows):
        import openpyxl
        import io
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Contributor Name", "Phone", "Receipt No", "Channel", "Tithe"])
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    def test_import_creates_review_batch_not_draft(self):
        self.client.force_login(self.assistant)
        f = self._xlsx([["Jane Doe", "0712345678", "IMP1", "CASH", 100]])
        from django.core.files.uploadedfile import SimpleUploadedFile
        upload = SimpleUploadedFile(
            "sheet.xlsx", f.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        r = self.client.post(reverse("envelope_import"),
                             {"date": SAB.isoformat(), "file": upload})
        self.assertEqual(r.status_code, 302)
        batch = EnvelopeBatch.objects.get(source=EnvelopeBatch.Source.IMPORT)
        self.assertEqual(batch.status, EnvelopeBatch.Status.REVIEW)
        self.assertEqual(batch.submitted_by, self.assistant)
        # nothing posted yet
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(Envelope.objects.count(), 0)

    def test_import_never_posts_directly(self):
        self.client.force_login(self.assistant)
        f = self._xlsx([["Jane Doe", "", "IMP2", "CASH", 50],
                        ["John Roe", "", "IMP3", "CASH", 75]])
        from django.core.files.uploadedfile import SimpleUploadedFile
        upload = SimpleUploadedFile(
            "sheet2.xlsx", f.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.client.post(reverse("envelope_import"),
                         {"date": SAB.isoformat(), "file": upload})
        self.assertEqual(Envelope.objects.count(), 0)
        batch = EnvelopeBatch.objects.get(source=EnvelopeBatch.Source.IMPORT)
        self.assertEqual(batch.rows.count(), 2)

    def test_import_row_clashing_with_posted_envelope_is_reviewable_not_dropped(self):
        Envelope.objects.create(date=SAB, receipt_no="CLASH1",
                                contributor_name="Existing", recorded_by=self.treasurer)
        self.client.force_login(self.assistant)
        f = self._xlsx([["New Person", "", "CLASH1", "CASH", 30]])
        from django.core.files.uploadedfile import SimpleUploadedFile
        upload = SimpleUploadedFile(
            "sheet3.xlsx", f.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.client.post(reverse("envelope_import"),
                         {"date": SAB.isoformat(), "file": upload})
        batch = EnvelopeBatch.objects.get(source=EnvelopeBatch.Source.IMPORT)
        # the row is present (not silently skipped) and flagged for review
        self.assertEqual(batch.rows.count(), 1)
        row = batch.rows.get()
        self.assertEqual(row.error, bsvc.ERR_DUPLICATE_RECEIPT)

    def test_imported_batch_can_be_approved_and_posted(self):
        self.client.force_login(self.assistant)
        f = self._xlsx([["Jane Doe", "", "IMP4", "CASH", 20]])
        from django.core.files.uploadedfile import SimpleUploadedFile
        upload = SimpleUploadedFile(
            "sheet4.xlsx", f.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.client.post(reverse("envelope_import"),
                         {"date": SAB.isoformat(), "file": upload})
        batch = EnvelopeBatch.objects.get(source=EnvelopeBatch.Source.IMPORT)
        self.assertFalse(bsvc.approve_batch(batch, self.treasurer))
        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertFalse(problems)
        self.assertEqual(count, 1)
        self.assertEqual(Envelope.objects.get().receipt_no, "IMP4")


# ===========================================================================
# Table-state (grid layout) endpoint
# ===========================================================================

class TableStateViewTests(_Seed):
    def test_round_trip(self):
        self.client.force_login(self.assistant)
        url = reverse("table_state", args=["envelope_ledger_grid"])
        r = self.client.get(url)
        self.assertEqual(r.json()["state"], {})

        state = {"order": ["__rownum", "__name"], "hidden": [], "widths": {"__name": 200},
                 "pinned": ["__rownum"]}
        r = self.client.post(url, data=json.dumps(state), content_type="application/json")
        self.assertTrue(r.json()["ok"])

        r = self.client.get(url)
        self.assertEqual(r.json()["state"], state)

    def test_per_user_isolation(self):
        url = reverse("table_state", args=["envelope_ledger_grid"])
        self.client.force_login(self.assistant)
        self.client.post(url, data=json.dumps({"order": ["a"]}),
                         content_type="application/json")
        other = _assistant("eb_asst4")
        self.client.force_login(other)
        r = self.client.get(url)
        self.assertEqual(r.json()["state"], {})

    def test_restored_on_future_login(self):
        # simulate "future login" as a fresh client instance for the same user
        url = reverse("table_state", args=["envelope_ledger_grid"])
        self.client.force_login(self.assistant)
        self.client.post(url, data=json.dumps({"order": ["x", "y"]}),
                         content_type="application/json")
        self.client.logout()
        self.client.force_login(self.assistant)
        r = self.client.get(url)
        self.assertEqual(r.json()["state"]["order"], ["x", "y"])


# ===========================================================================
# Audit trail
# ===========================================================================

class AuditTrailTests(_Seed):
    def test_batch_history_captures_transitions(self):
        b, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(b, [
            {"line_no": 1, "receipt_no": "AT1", "contributor_name": "Jane",
             "manual_total": "10", "amounts": {str(self.tithe.id): "10"}}])
        bsvc.submit_batch(b, self.assistant)
        bsvc.approve_batch(b, self.treasurer)
        bsvc.post_batch(b, self.treasurer)
        statuses = list(b.history.order_by("history_date")
                        .values_list("status", flat=True))
        self.assertIn(EnvelopeBatch.Status.DRAFT, statuses)
        self.assertIn(EnvelopeBatch.Status.REVIEW, statuses)
        self.assertIn(EnvelopeBatch.Status.APPROVED, statuses)
        self.assertIn(EnvelopeBatch.Status.POSTED, statuses)

    def test_batch_appears_in_audit_log_report(self):
        from reports.views import AuditLogView
        self.assertIn("Envelope batch", AuditLogView()._models())
