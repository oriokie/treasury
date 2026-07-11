"""Test for a critical bug (v2.44): EnvelopeBatchRow.dev_group (and
member_id) were silently never captured from the ledger UI.

Root cause: envelopes.services.batches.autosave_rows built
{model.id: model} lookup dicts (int keys, since Django PKs are int) but
looked members/dev_groups up using the RAW value straight from the client
JSON payload — always a STRING (a <select>'s .value, a hidden <input>'s
.value are strings in every browser). `{4: obj}.get("4")` returns None:
Python dict keys are type-sensitive, "4" != 4, even though the SAME string
value works fine in the ORM's own `pk__in` filter immediately above it
(Django coerces query parameters, plain dict lookups do not). This silently
dropped dev_group on every single row, regardless of the fund's category or
how many Development funds existed — the "is_development picker renders
correctly" fix from the previous release only ensured the UI *offered* a
correct choice; it never reached the database.

Subgroup rekeying was unaffected by this specific bug (its ..amounts dict
is keyed by department-id STRINGS throughout, matching how the amounts
JSONField is stored — no int-keyed dict lookup step existed in that path),
which is why subgroups "worked as expected" while dev_group silently didn't.
"""
import datetime as dt
import json
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department, DevelopmentGroup
from envelopes.models import EnvelopeBatch
from envelopes.services import batches as bsvc
from giving.models import Transaction
from members.models import Member


def _assistant(username="dg_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


def _treasurer(username="dg_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


SAB = dt.date(2026, 6, 6)


class DevGroupStringIdCaptureTests(TestCase):
    """The exact reported bug: dev_group_id arrives as a string (as it
    always does from a browser), and must still be captured."""

    def setUp(self):
        self.u = _assistant()
        self.dev_fund = Department.objects.create(
            name="DG Fund", fund_type="LOCAL", category="DEVELOPMENT")
        self.grp = DevelopmentGroup.objects.create(number=501, name="Group 501",
                                                   active=True)

    def test_string_dev_group_id_captured_via_service(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "DG1", "contributor_name": "Jane",
             "manual_total": "500", "amounts": {str(self.dev_fund.id): "500"},
             "dev_group_id": str(self.grp.id)}])   # string, matching a real payload
        row = batch.rows.get()
        self.assertEqual(row.dev_group_id, self.grp.id)

    def test_string_dev_group_id_captured_via_http_endpoint(self):
        self.client.force_login(self.u)
        payload = {"batch_id": None, "date": SAB.isoformat(), "rows": [
            {"line_no": 1, "receipt_no": "DG2", "contributor_name": "Jane",
             "manual_total": "500", "amounts": {str(self.dev_fund.id): "500"},
             "dev_group_id": str(self.grp.id)}]}
        r = self.client.post(reverse("envelope_batch_autosave"),
                             data=json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        batch = EnvelopeBatch.objects.get(pk=r.json()["batch_id"])
        self.assertEqual(batch.rows.get().dev_group_id, self.grp.id)

    def test_dev_group_survives_all_the_way_to_the_posted_transaction(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "DG3", "contributor_name": "Jane",
             "manual_total": "500", "amounts": {str(self.dev_fund.id): "500"},
             "dev_group_id": str(self.grp.id)}])
        self.assertFalse(bsvc.submit_batch(batch, self.u))
        tr = _treasurer()
        self.assertFalse(bsvc.approve_batch(batch, tr))
        problems, count = bsvc.post_batch(batch, tr)
        self.assertFalse(problems)
        self.assertEqual(count, 1)
        txn = Transaction.objects.get(reference__contains="DG3")
        self.assertEqual(txn.dev_group_id, self.grp.id)
        from envelopes.models import EnvelopeLine
        line = EnvelopeLine.objects.get(envelope__receipt_no="DG3")
        self.assertEqual(line.dev_group_id, self.grp.id)

    def test_empty_string_dev_group_id_stays_none(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "DG4", "contributor_name": "Jane",
             "manual_total": "100", "amounts": {str(self.dev_fund.id): "100"},
             "dev_group_id": ""}])
        row = batch.rows.get()
        self.assertIsNone(row.dev_group_id)

    def test_none_dev_group_id_stays_none(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "DG5", "contributor_name": "Jane",
             "manual_total": "100", "amounts": {str(self.dev_fund.id): "100"},
             "dev_group_id": None}])
        row = batch.rows.get()
        self.assertIsNone(row.dev_group_id)

    def test_garbage_dev_group_id_does_not_crash(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "DG6", "contributor_name": "Jane",
             "manual_total": "100", "amounts": {str(self.dev_fund.id): "100"},
             "dev_group_id": "not-a-number"}])
        row = batch.rows.get()
        self.assertIsNone(row.dev_group_id)

    def test_nonexistent_dev_group_id_stays_none_not_error(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "DG7", "contributor_name": "Jane",
             "manual_total": "100", "amounts": {str(self.dev_fund.id): "100"},
             "dev_group_id": "999999"}])
        row = batch.rows.get()
        self.assertIsNone(row.dev_group_id)


class MemberIdStringCaptureTests(TestCase):
    """The same fix, same bug class, for member_id (the autocomplete's
    hidden input — also always a string)."""

    def setUp(self):
        self.u = _assistant()
        self.fund = Department.objects.create(name="MID Fund", fund_type="LOCAL")

    def test_string_member_id_captured(self):
        m = Member.objects.create(name="Jane Doe", phone="0712345678")
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "MID1", "contributor_name": "Jane Doe",
             "manual_total": "100", "amounts": {str(self.fund.id): "100"},
             "member_id": str(m.id)}])
        row = batch.rows.get()
        self.assertEqual(row.member_id, m.id)

    def test_empty_member_id_stays_none(self):
        batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "MID2", "contributor_name": "Someone",
             "manual_total": "100", "amounts": {str(self.fund.id): "100"},
             "member_id": ""}])
        row = batch.rows.get()
        self.assertIsNone(row.member_id)
