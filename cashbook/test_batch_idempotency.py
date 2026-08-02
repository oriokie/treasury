"""A batch of expenses sent twice is recorded once.

The reported failure: two lines entered on /expenses/batch/, four expenses in
the books. Saving a stack of receipts takes a moment, and the second click
during that moment posted the whole form again.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense
from core.models import FormSubmission
from core.roles import TREASURER
from core.services import idempotency
from departments.models import Department


def _treasurer(username="batch_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class _Base(TestCase):
    def setUp(self):
        self.user = _treasurer()
        self.client.force_login(self.user)
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        self.url = reverse("expense_batch")

    def payload(self, token):
        return {
            "submit_token": token,
            "date": "2026-07-20", "department": str(self.fund.pk),
            "claimant": "J. Mwangi", "method": "CASH", "category": "MAINTENANCE",
            "line_description": ["Cement", "Nails"],
            "line_amount": ["4000", "1500"],
            "line_category": ["", ""],
            "line_charge": ["", ""],
        }


class TokenTests(_Base):
    def test_the_form_renders_a_token(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="submit_token"')
        self.assertTrue(r.context["submit_token"])

    def test_each_render_gets_its_own_token(self):
        a = self.client.get(self.url).context["submit_token"]
        b = self.client.get(self.url).context["submit_token"]
        self.assertNotEqual(a, b)


class DuplicateSubmitTests(_Base):
    def test_one_submission_records_each_line_once(self):
        token = self.client.get(self.url).context["submit_token"]
        self.client.post(self.url, self.payload(token))
        self.assertEqual(Expense.objects.count(), 2)

    def test_the_same_form_sent_twice_records_one_batch(self):
        """The reported bug: 2 lines in, 4 expenses out."""
        token = self.client.get(self.url).context["submit_token"]
        self.client.post(self.url, self.payload(token))
        self.client.post(self.url, self.payload(token))
        self.assertEqual(Expense.objects.count(), 2)
        self.assertEqual(
            sorted(Expense.objects.values_list("description", flat=True)),
            ["Cement", "Nails"])

    def test_the_repeat_says_so_rather_than_failing_silently(self):
        token = self.client.get(self.url).context["submit_token"]
        self.client.post(self.url, self.payload(token))
        r = self.client.post(self.url, self.payload(token), follow=True)
        self.assertContains(r, "already saved")

    def test_a_genuine_second_batch_still_saves(self):
        """A church may pay the same claimant the same amount twice in a day.
        A new form means a new token, and it must go through."""
        first = self.client.get(self.url).context["submit_token"]
        self.client.post(self.url, self.payload(first))
        second = self.client.get(self.url).context["submit_token"]
        self.client.post(self.url, self.payload(second))
        self.assertEqual(Expense.objects.count(), 4)

    def test_totals_are_right_after_a_double_send(self):
        token = self.client.get(self.url).context["submit_token"]
        self.client.post(self.url, self.payload(token))
        self.client.post(self.url, self.payload(token))
        total = sum(e.amount for e in Expense.objects.all())
        self.assertEqual(total, Decimal("5500"))


class ClaimTests(TestCase):
    def setUp(self):
        self.user = _treasurer("claim_tr")

    def _request(self, token):
        from django.test import RequestFactory
        req = RequestFactory().post("/", {"submit_token": token} if token else {})
        req.user = self.user
        return req

    def test_first_claim_wins_and_the_second_loses(self):
        req = self._request("abc123")
        self.assertTrue(idempotency.claim(req, view="t"))
        self.assertFalse(idempotency.claim(self._request("abc123"), view="t"))

    def test_a_form_with_no_token_is_never_blocked(self):
        """The guard is opt-in per form; a missing token must not stop a save."""
        self.assertTrue(idempotency.claim(self._request(None), view="t"))
        self.assertTrue(idempotency.claim(self._request(None), view="t"))

    def test_different_tokens_are_independent(self):
        self.assertTrue(idempotency.claim(self._request("one"), view="t"))
        self.assertTrue(idempotency.claim(self._request("two"), view="t"))
        self.assertEqual(FormSubmission.objects.count(), 2)

    def test_old_claims_are_pruned(self):
        from django.utils import timezone
        idempotency.claim(self._request("old"), view="t")
        FormSubmission.objects.filter(token="old").update(
            created_at=timezone.now() - idempotency.RETENTION - dt.timedelta(days=1))
        idempotency.claim(self._request("new"), view="t")
        self.assertFalse(FormSubmission.objects.filter(token="old").exists())
        self.assertTrue(FormSubmission.objects.filter(token="new").exists())
