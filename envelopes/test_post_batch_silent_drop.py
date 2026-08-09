"""Regression coverage for docs/recommendations.md #63 — the shape of bug
where an approved batch posts SHORT and every downstream total still balances.

`post_batch` used to resolve its fund dict as
`Department.objects.filter(active=True)`, and `_expand_lines` used to `continue`
past any `amounts` key that dict could not resolve. Between the two, a fund
closed in the days between a batch being approved and actually posted made its
line evaporate at Post: no error, no row flagged, the receipt number consumed
and the envelope saved short — or, if it was the row's only fund, saved as a
ZERO-TOTAL envelope with no ledger entry at all, for cash a treasurer had
physically counted and reconciled. Nothing anywhere disagreed afterwards, which
is exactly why it could sit undetected.

The fix has two halves and this file guards both, because only the second one
closes the whole class:

* `post_batch` resolves funds and splits WITHOUT the active filter. Closing a
  fund is meant to stop NEW money being entered against it, not to un-post an
  approved batch that already references it — the money was given before the
  fund closed, and `Department.status`'s own help text already promises that
  closed accounts "stay in historical reports".
* `_expand_lines` now enforces one invariant for every caller: a nonzero amount
  becomes lines summing to EXACTLY it, or `UnpostableAllocation` is raised and
  the whole batch rolls back. A deleted fund, a `split:` key naming nothing, a
  key that is not a number, a split with no components — all the same failure,
  all refused. An envelope that will not post is a phone call; an envelope that
  posts short is a hole nobody finds.

The last two classes pin the gate the first half leans on, from both ends.
Letting an approved batch post into a closed fund is only safe while closing a
fund still stops NEW money going in, so that promise has to hold: the fund must
stay off the entry grid, AND the server must refuse it at Submit/Approve rather
than trusting the grid to be the only way rows are made. It is not — a stale
browser tab, the spreadsheet importer and a replayed autosave all reach
`autosave_rows` without ever having read the catalogue.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from envelopes.models import Envelope, EnvelopeBatch
from envelopes.services import batches as bsvc
from envelopes.services.posting import UnpostableAllocation, _expand_lines
from giving.models import SplitComponent, SplitFund, Transaction

SAB = dt.date(2026, 6, 6)   # a Saturday


class _ApprovedBatchSeed(TestCase):
    """Everything these tests share: two funds and the ability to walk a batch
    all the way to APPROVED, which is the state the bug needs — the world is
    allowed to change underneath a batch only after it has been approved and
    before someone clicks Post."""

    def setUp(self):
        self.assistant = User.objects.create_user("sd_asst", password="x")
        self.assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.treasurer = User.objects.create_user("sd_tr", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.camp = Department.objects.create(name="Camp Meeting", fund_type="LOCAL")

    def _approved(self, amounts, total, receipt="SD1", name="Jane Doe"):
        batch, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": receipt, "contributor_name": name,
             "channel": "CASH", "manual_total": total, "amounts": amounts},
        ])
        self.assertFalse(bsvc.submit_batch(batch, self.assistant))
        self.assertFalse(bsvc.approve_batch(batch, self.treasurer))
        return batch

    def _close(self, dept):
        """Close a fund the way a treasurer does it — through `status`, which
        is what `Department.save` derives `active` from. Setting `active`
        directly would test a state the application itself cannot produce."""
        dept.status = Department.Status.CLOSED
        dept.save()
        dept.refresh_from_db()
        self.assertFalse(dept.active)

    def _approved_against_a_doomed_fund(self, amount, **kw):
        """An APPROVED row holding money on a fund key that resolves to
        nothing by the time Post runs — built by approving against a real
        fund and then deleting it.

        Not by seeding a made-up id: since `closed_or_unknown_columns` the
        Submit/Approve gate refuses a column the register does not offer, so a
        made-up id can no longer get past the front door and a test that used
        one would be exercising a state the app cannot reach. Making the fund
        vanish AFTER approval is also the honest shape of the failure — the
        row was legitimately entered and approved, and the world moved
        underneath it."""
        doomed = Department.objects.create(name="Doomed Fund",
                                           fund_type="LOCAL")
        batch = self._approved({str(doomed.id): amount}, amount, **kw)
        doomed.delete()
        return batch


class FundClosedBetweenApprovalAndPostTests(_ApprovedBatchSeed):
    def test_a_fund_closed_after_approval_still_posts_the_money_given_to_it(self):
        """The money was given to the fund while it was open, so it posts. This
        is the two-fund shape of the bug: the envelope used to save SHORT — 100
        instead of 150 — and because the shortfall never appeared anywhere as a
        difference, every report still added up."""
        batch = self._approved(
            {str(self.tithe.id): "100", str(self.camp.id): "50"}, "150")
        self._close(self.camp)

        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertFalse(problems)
        self.assertEqual(count, 1)

        env = Envelope.objects.get(receipt_no="SD1")
        self.assertEqual(env.total, Decimal("150"))
        self.assertEqual(
            Transaction.objects.get(department=self.camp).amount, Decimal("50"))

    def test_a_row_whose_only_fund_closed_does_not_post_a_zero_total_envelope(self):
        """The worst shape of #63: the closed fund was the row's ONLY fund, so
        `recompute_total()` produced a zero-total envelope against a real
        receipt number with no ledger entry behind it — cash the treasurer had
        already counted and reconciled, recorded as nothing."""
        batch = self._approved({str(self.camp.id): "80"}, "80", receipt="SD2")
        self._close(self.camp)

        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertFalse(problems)
        self.assertEqual(count, 1)

        env = Envelope.objects.get(receipt_no="SD2")
        self.assertEqual(env.total, Decimal("80"))
        self.assertEqual(env.lines.count(), 1)
        self.assertEqual(Transaction.objects.count(), 1)

    def test_a_split_fund_deactivated_after_approval_still_posts_both_halves(self):
        """`splits` lost its active filter for the same reason `funds` did —
        a split deactivated mid-flight was the identical silent drop, just one
        level of indirection further down."""
        sf = SplitFund.objects.create(name="Combined Offering")
        SplitComponent.objects.create(split_fund=sf, department=self.tithe,
                                      percent=Decimal("50"))
        SplitComponent.objects.create(split_fund=sf, department=self.camp,
                                      percent=Decimal("50"))
        batch = self._approved({f"split:{sf.id}": "200"}, "200", receipt="SD3")
        sf.active = False
        sf.save(update_fields=["active"])

        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertFalse(problems)
        self.assertEqual(count, 1)
        self.assertEqual(Envelope.objects.get(receipt_no="SD3").total,
                         Decimal("200"))
        self.assertEqual(
            sorted(t.amount for t in Transaction.objects.all()),
            [Decimal("100"), Decimal("100")])


class UnresolvableColumnRefusesToPostTests(_ApprovedBatchSeed):
    """The half that closes the CLASS. Un-filtering the fund lookup fixes the
    one path that exposed #63; refusing to post an amount that cannot be fully
    allocated is what stops the next one being found the same way."""

    def test_an_amount_against_a_fund_that_no_longer_exists_refuses_to_post(self):
        """A fund id that resolves to nothing at all — the row was approved
        against it, so validation is already behind us and Post is the last
        chance to notice. Nothing may be written, and the message has to name
        whose envelope is stuck: 'a row failed' sends a treasurer hunting."""
        batch = self._approved_against_a_doomed_fund(
            "120", receipt="SD4", name="Peter Otieno")

        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertEqual(count, 0)
        self.assertTrue(problems)
        self.assertIn("Peter Otieno", problems[0])
        self.assertIn("SD4", problems[0])

        # all or nothing: the ledger is untouched and the batch is exactly as
        # it was, so posting can simply be retried once the fund is sorted out
        self.assertEqual(Envelope.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.APPROVED)

    def test_a_split_column_with_no_components_refuses_to_post(self):
        """A split fund set up but never given its component percentages
        divides an amount into nothing at all. `split()` returns [], which
        under the old code meant the money simply was not posted."""
        sf = SplitFund.objects.create(name="Unconfigured Split")
        batch = self._approved({f"split:{sf.id}": "60"}, "60", receipt="SD5")

        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertEqual(count, 0)
        self.assertTrue(problems)
        self.assertEqual(Envelope.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_a_partially_allocated_row_takes_its_whole_batch_down_with_it(self):
        """Deliberately all-or-nothing. Posting the clean rows and leaving the
        broken one behind would split a Sabbath's takings across two states and
        leave the treasurer reconciling a batch that is half in the ledger."""
        doomed = Department.objects.create(name="Doomed Fund", fund_type="LOCAL")
        batch, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": "SD6", "contributor_name": "Clean Row",
             "channel": "CASH", "manual_total": "40",
             "amounts": {str(self.tithe.id): "40"}},
            {"line_no": 2, "receipt_no": "SD7", "contributor_name": "Broken Row",
             "channel": "CASH", "manual_total": "40",
             "amounts": {str(doomed.id): "40"}},
        ])
        self.assertFalse(bsvc.submit_batch(batch, self.assistant))
        self.assertFalse(bsvc.approve_batch(batch, self.treasurer))
        doomed.delete()   # only reachable after approval — see the seed helper

        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertEqual(count, 0)
        self.assertTrue(problems)
        self.assertEqual(Envelope.objects.count(), 0)   # not even the clean row


class MisconfiguredSplitRefusesToPostTests(_ApprovedBatchSeed):
    """A split whose components don't total 100% is the one unresolvable column
    that did not come out as a refusal. `SplitFund.split` correctly refuses to
    divide it — but by raising *django's* ValidationError, which `post_batch`
    does not catch, so a misconfigured 40/40 split used to answer Post with a
    500: no problem list, no row named, and a treasurer with no way to tell
    whether the batch had gone in. It is the same failure as a deleted fund or
    an empty split and now reads the same way."""

    def test_a_split_that_does_not_total_100_is_refused_by_name_not_a_500(self):
        sf = SplitFund.objects.create(name="Combined Offering")
        SplitComponent.objects.create(split_fund=sf, department=self.tithe,
                                      percent=Decimal("40"))
        SplitComponent.objects.create(split_fund=sf, department=self.camp,
                                      percent=Decimal("40"))
        batch = self._approved({f"split:{sf.id}": "200"}, "200",
                               receipt="SD8", name="Mary Wanjiru")

        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertEqual(count, 0)
        self.assertTrue(problems)
        # whose envelope, which receipt, and the split's own explanation of
        # what is wrong with it — all three, or the treasurer is left hunting
        self.assertIn("Mary Wanjiru", problems[0])
        self.assertIn("SD8", problems[0])
        self.assertIn("Combined Offering", problems[0])
        self.assertIn("80", problems[0])

        # all or nothing, exactly as for every other unresolvable column: the
        # 40% that COULD be divided must not land in the ledger on its own
        self.assertEqual(Envelope.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.APPROVED)


class ExpandLinesInvariantTests(TestCase):
    """The invariant tested where it is defined, not only through `post_batch`.
    `_expand_lines` is the one place every posting path funnels through, so
    this is the assertion that has to hold for callers that do not exist yet."""

    def setUp(self):
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")

    def test_every_nonzero_amount_is_either_fully_allocated_or_raises(self):
        lines = _expand_lines({str(self.tithe.id): "75"}, {self.tithe.id: self.tithe}, {})
        self.assertEqual(lines, [(self.tithe, Decimal("75"))])

        for key in ("987654", "split:987654", "not-a-number", "split:oops"):
            with self.subTest(key=key):
                with self.assertRaises(UnpostableAllocation):
                    _expand_lines({key: "75"}, {self.tithe.id: self.tithe}, {})

    def test_a_split_that_does_not_total_100_raises_the_posting_refusal(self):
        """Pinned at the invariant, not only through `post_batch`: the caller
        contract is that this function raises `UnpostableAllocation` and
        nothing else for a column it cannot fully allocate. If django's own
        ValidationError escapes again, this errors out instead of failing
        cleanly — which is precisely what a 500 in production looked like."""
        sf = SplitFund.objects.create(name="Half A Split")
        SplitComponent.objects.create(split_fund=sf, department=self.tithe,
                                      percent=Decimal("60"))
        with self.assertRaises(UnpostableAllocation):
            _expand_lines({f"split:{sf.id}": "100"},
                          {self.tithe.id: self.tithe}, {sf.id: sf})

    def test_a_blank_or_zero_cell_is_still_skipped_without_complaining(self):
        """The new refusal must not turn an ordinary empty grid cell into a
        posting failure. It is safe to skip these precisely because
        `recompute_row_total` reads them with the very same `_amount`: a cell
        the poster sees as nothing is a cell the envelope-total check already
        saw as nothing, so a real figure typed there surfaces as a
        TOTAL_MISMATCH long before anyone reaches Post."""
        lines = _expand_lines(
            {str(self.tithe.id): "10", "987654": "", "987655": "0",
             "987656": None, "987657": "  "},
            {self.tithe.id: self.tithe}, {})
        self.assertEqual(lines, [(self.tithe, Decimal("10"))])


class ClosingAFundStillBlocksNewEntriesTests(TestCase):
    """The behaviour that must NOT have regressed. `post_batch` no longer cares
    whether a fund is active, so the promise that closing a fund stops new money
    being entered against it rests on the column catalogue instead. Pinned here
    so it cannot be lost quietly."""

    def test_a_closed_fund_is_no_longer_offered_as_an_entry_column(self):
        from envelopes.services.posting import column_catalog
        fund = Department.objects.create(name="Retired Project", fund_type="LOCAL")
        self.assertIn(str(fund.id), {c["key"] for c in column_catalog()})

        fund.status = Department.Status.CLOSED
        fund.save()
        self.assertNotIn(str(fund.id), {c["key"] for c in column_catalog()})


class ClosedColumnIsRefusedServerSideTests(_ApprovedBatchSeed):
    """The same promise, enforced where it actually holds. Keeping a closed
    fund out of `column_catalog()` only stops the browser offering it, and the
    browser is not the only thing that reaches `autosave_rows`: a tab opened
    before the fund was closed still has the old columns rendered, the
    spreadsheet importer maps sheet headings onto funds without consulting the
    grid at all, and an autosave POST can simply be replayed. Any of those
    could seat live money on a closed fund and walk it to POSTED — the exact
    thing closing the fund was meant to prevent — because until
    `closed_or_unknown_columns` there was no server-side check anywhere.

    The gate is at Submit and Approve, where a row is created or accepted, and
    deliberately nowhere near Post; the last test here is that asymmetry, which
    is the #63 fix itself and must never be "tidied up" into symmetry."""

    def _draft_with(self, amounts, total="60", receipt="CL1",
                    name="Grace Akinyi"):
        batch, _ = bsvc.get_or_create_draft(self.assistant, None, SAB)
        bsvc.autosave_rows(batch, [
            {"line_no": 1, "receipt_no": receipt, "contributor_name": name,
             "channel": "CASH", "manual_total": total, "amounts": amounts}])
        return batch

    def test_submitting_money_on_a_closed_fund_is_refused_and_names_it(self):
        """A stale tab, in one line: the grid rendered Camp Meeting as a
        column, the fund closed, and the autosave that follows still carries
        it. The refusal has to name the contributor, the receipt and the fund
        — the treasurer is being asked to move that money somewhere, and
        cannot without knowing where it is."""
        batch = self._draft_with({str(self.camp.id): "60"})
        self._close(self.camp)

        problems = bsvc.submit_batch(batch, self.assistant)
        self.assertTrue(problems)
        self.assertIn("Grace Akinyi", problems[0])
        self.assertIn("CL1", problems[0])
        self.assertIn("Camp Meeting", problems[0])

        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.DRAFT)

    def test_a_fund_closed_between_submit_and_approve_blocks_approval(self):
        """Approve re-runs the gate rather than trusting Submit's verdict,
        because the whole reason this workflow re-validates at every step is
        that time passes between them."""
        batch = self._draft_with({str(self.camp.id): "60"}, receipt="CL2")
        self.assertFalse(bsvc.submit_batch(batch, self.assistant))
        self._close(self.camp)

        problems = bsvc.approve_batch(batch, self.treasurer)
        self.assertTrue(problems)
        self.assertIn("Camp Meeting", problems[0])
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.REVIEW)

    def test_a_deactivated_split_column_is_refused_at_submit_too(self):
        """A split is a column like any other and disappears from the
        catalogue the same way, so it has to be refused the same way — named,
        not reported as a bare `split:4`."""
        sf = SplitFund.objects.create(name="Retired Combined")
        SplitComponent.objects.create(split_fund=sf, department=self.tithe,
                                      percent=Decimal("50"))
        SplitComponent.objects.create(split_fund=sf, department=self.camp,
                                      percent=Decimal("50"))
        batch = self._draft_with({f"split:{sf.id}": "60"}, receipt="CL3")
        sf.active = False
        sf.save(update_fields=["active"])

        problems = bsvc.submit_batch(batch, self.assistant)
        self.assertTrue(problems)
        self.assertIn("Retired Combined", problems[0])

    def test_an_empty_cell_on_a_closed_fund_is_not_money_and_does_not_block(self):
        """The grid posts a key for every open column, most of them blank, so
        a gate that looked at keys rather than at money would have refused
        every batch entered on the Sabbath a fund happened to close. Only cells
        `_amount` reads as a figure count — the same test `_expand_lines` and
        `recompute_row_total` apply, so all three agree on what "money" is."""
        batch = self._draft_with(
            {str(self.tithe.id): "60", str(self.camp.id): ""}, receipt="CL4")
        self._close(self.camp)

        self.assertFalse(bsvc.submit_batch(batch, self.assistant))
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.REVIEW)

    def test_the_gate_is_absent_at_post_which_is_the_whole_of_63(self):
        """Both halves of the rule in one assertion pair, on one batch: the
        column would be refused for a NEW entry, and the already-approved row
        holding it still posts. Symmetry here would re-break #63 — an approved
        Sabbath's counted cash stranded because a fund closed on the Monday."""
        batch = self._approved({str(self.camp.id): "60"}, "60", receipt="CL5")
        self._close(self.camp)

        self.assertTrue(bsvc.closed_or_unknown_columns(batch))
        self.assertFalse(bsvc.validate_batch_for_post(batch))

        problems, count = bsvc.post_batch(batch, self.treasurer)
        self.assertFalse(problems)
        self.assertEqual(count, 1)
        self.assertEqual(Envelope.objects.get(receipt_no="CL5").total,
                         Decimal("60"))
