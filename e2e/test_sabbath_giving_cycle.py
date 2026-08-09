"""A Sabbath's giving, from the plate to the fund balance.

The real path a church's Sabbath takes, in the order a church takes it:

    the offering is counted  ->  the envelopes are keyed into the entry grid
    ->  the sheet is submitted for review  ->  a second officer approves it
    ->  it is posted  ->  the funds hold what was given

Every one of those steps has unit tests. What none of them can show is that the
money is the same figure at the end as it was in the plate — and that is exactly
where this application's envelope defects have lived. Issue #63 was a line
silently dropped at posting: the envelope was still created, the receipt number
was still consumed, and cash a treasurer had physically counted simply was not
in the ledger, with every downstream total still reconciling. That is the shape
this file is built to catch, so its assertions are deliberately redundant: the
same Sabbath's money is read from the batch, from the envelope rows, from the
ledger transactions, from the fund summary and from the Collections Summary,
and all five must agree.

It also walks the half of the rule that fails quietly. A batch that is still
DRAFT, or submitted and awaiting review, must contribute *exactly nothing* to
any fund — and money missing from a report looks identical to money never
given, so nothing but an explicit assertion notices when a staging worksheet
starts leaking into the accounts.

Two defects this file found are now FIXED and are guarded here: posting used to
land the treasurer on a month overview that could not contain the Sabbath just
posted, and a fund closed in the #63 window kept its money in the ledger but
disappeared from the fund summary, so two reports of the same Sabbath disagreed.
Both tests were written while the defects were live and carried an
`expectedFailure` marker until the fix landed.
"""
import datetime as dt
import json
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse

from core.models import SiteConfig
from departments.models import Department
from envelopes.models import CountSession, Envelope, EnvelopeBatch, EnvelopeLine
from giving.models import Transaction
from members.models import Member

from .base import BusinessWorkflowTest, WorkflowError

#: The Sabbath being walked. A Saturday inside the harness's reporting period,
#: so the period-end fund assertions and the month/year report assertions all
#: see it.
SABBATH = dt.date(2026, 7, 11)
MONTH_START = dt.date(2026, 7, 1)
MONTH_END = dt.date(2026, 7, 31)

# What each envelope holds, split across the fund columns exactly as the
# cashier keys it: (receipt, contributor, channel, {fund attr: amount}).
# Deliberately populated — several contributors, several funds, cash AND a
# bank-channel envelope — because an envelope workflow proved on one cash row
# against one fund proves the easy third of it (documented failure #125).
PLATE = [
    ("R-4101", "Grace Wanjiru",  "CASH", {"tithe": "5000", "offering": "800"}),
    ("R-4102", "Peter Otieno",   "CASH", {"tithe": "2500"}),
    ("R-4103", "Mary Achieng",   "CASH", {"offering": "1200", "camp": "500"}),
    ("R-4104", "Samuel Kiprono", "CASH", {"tithe": "3000", "offering": "450",
                                          "camp": "250"}),
    # given by M-Pesa during the week and written up on the same sheet: it is
    # income like any other, but it is NOT in the cash box being counted.
    ("R-4105", "Esther Njeri",   "BANK", {"tithe": "10000"}),
]

CASH_IN_THE_PLATE = Decimal("13700.00")     # the four CASH envelopes
BANK_ON_THE_SHEET = Decimal("10000.00")     # the one BANK envelope
SABBATH_TOTAL = CASH_IN_THE_PLATE + BANK_ON_THE_SHEET

# What each fund ends the Sabbath holding.
TITHE_TOTAL = Decimal("20500.00")           # 5000 + 2500 + 3000 + 10000
OFFERING_TOTAL = Decimal("2450.00")         # 800 + 1200 + 450
CAMP_TOTAL = Decimal("750.00")              # 500 + 250


def money(value):
    """A figure at the two decimal places every money column in this
    application stores. `assert_agree` compares figures as strings, so
    ``Decimal("13700")`` and the ``Decimal("13700.00")`` that comes back out of
    a ``Sum()`` read as a disagreement when they are the same shilling. Scale
    is presentation; this normalises it so a reported disagreement is a real
    one."""
    return Decimal(value).quantize(Decimal("0.01"))


class SabbathGivingCycle(BusinessWorkflowTest):

    def setUp(self):
        super().setUp()
        cfg = SiteConfig.get()
        # Maker-checker in its strict form, which is the whole point of the
        # batch pipeline: the officer who keys a Sabbath in may not be the one
        # who passes it into the accounts.
        cfg.require_different_approver = True
        cfg.save()

        # -- the funds the plate is split across ------------------------------
        # Background state, created directly: a church's fund register exists
        # long before this Sabbath and is not what this workflow creates. The
        # harness's own LOCAL fund cannot be used here — it is called "Church
        # Building", and the envelope column catalogue deliberately excludes
        # any fund with "building" in its name, so money keyed against it would
        # be rejected at Submit as a closed column. That is correct behaviour,
        # and not this workflow's subject.
        self.tithe = self.trust_fund                      # "Tithe", TRUST
        self.offering = Department.objects.create(
            name="Combined Offering", slug="wf-combined",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.OFFERING)
        self.camp = Department.objects.create(
            name="Camp Meeting", slug="wf-camp",
            fund_type=Department.FundType.TRUST,
            category=Department.Category.TRUST)
        self.funds = {"tithe": self.tithe, "offering": self.offering,
                      "camp": self.camp}

        # -- the congregation --------------------------------------------------
        # Real member rows with real relations, so the envelopes attach to
        # people the way they do on a live register rather than to bare names.
        self.members = {}
        for name, phone in [("Grace Wanjiru", "0722000001"),
                            ("Peter Otieno", "0722000002"),
                            ("Mary Achieng", "0722000003"),
                            ("Samuel Kiprono", "0722000004"),
                            ("Esther Njeri", "0722000005")]:
            # keyed by the name as the cashier writes it; the register itself
            # stores names uppercased
            self.members[name] = Member.objects.create(name=name, phone=phone)

        # -- the officers ------------------------------------------------------
        self.cashier = self.acting_as(self.assistant)     # keys the sheet in
        self.office = self.acting_as(self.treasurer)      # reviews and posts

    # ==================================================================
    # helpers — the two grid interactions the harness has no verb for
    # ==================================================================

    def _rows_payload(self, plate=None, first_line=1):
        """The grid's own row shape, as the browser sends it."""
        rows = []
        for i, (receipt, name, channel, split) in enumerate(plate or PLATE,
                                                            start=first_line):
            amounts = {str(self.funds[k].id): v for k, v in split.items()}
            total = sum(Decimal(v) for v in split.values())
            member = self.members.get(name)
            rows.append({
                "line_no": i, "receipt_no": receipt,
                "receipt_no_overridden": True,
                "contributor_name": name,
                "member_id": str(member.id) if member else "",
                "phone": member.phone if member else "",
                "channel": channel, "dev_group_id": "",
                "manual_total": str(total), "amounts": amounts,
            })
        return rows

    def _key_in(self, client, rows, batch_id=None, date=SABBATH,
                allow_row_errors=False):
        """Type rows into the entry grid, the way the grid really saves them.

        The envelope sheet has no form POST of its own — every keystroke goes
        to `envelope_batch_autosave`, and the unload-safety-net path posts that
        same JSON form-encoded (the `payload` field) because `sendBeacon`
        cannot set a content type. That is the shape used here, so this runs
        through `submit()` like every other write in the suite.

        A JSON endpoint has the same false-green as a form: HTTP 200 with
        `{"ok": false}` inside it, or 200 with every row flagged and nothing
        usable saved. `submit()` cannot see either (there is no bound form in
        the context), so this checks the body itself.
        """
        payload = {"batch_id": batch_id, "date": date.isoformat(), "rows": rows}
        response = self.submit(client, "envelope_batch_autosave",
                               {"payload": json.dumps(payload)})
        body = json.loads(response.content.decode())
        if not body.get("ok"):
            raise WorkflowError(
                f"The entry grid refused to save the sheet: "
                f"{body.get('error')!r}. Nothing was keyed in.")
        if body.get("errors") and not allow_row_errors:
            raise WorkflowError(
                f"The grid saved the sheet with rows flagged as unusable: "
                f"{body['errors']}. The cashier cannot submit this.")
        return body

    def _walk_to_approved(self, plate=None):
        """Key the sheet in, submit it, have it approved — everything up to,
        and deliberately short of, posting."""
        batch_id = self._key_in(self.cashier, self._rows_payload(plate))["batch_id"]
        self.submit(self.cashier, "envelope_batch_submit", {}, args=[batch_id])
        self.submit(self.office, "envelope_batch_approve", {}, args=[batch_id])
        batch = EnvelopeBatch.objects.get(pk=batch_id)
        self.assertEqual(batch.status, EnvelopeBatch.Status.APPROVED)
        return batch

    def _post(self, batch, allow_refusal=False):
        return self.submit(self.office, "envelope_batch_post", {},
                           args=[batch.pk], allow_form_errors=allow_refusal)

    def _agree(self, description, **figures):
        """`assert_agree`, with every figure put on the same two-decimal
        footing first — see `money()`."""
        return self.assert_agree(
            description, **{k: money(v) for k, v in figures.items()})

    # -- readings of "this Sabbath's money", each by a different route --------

    def _envelope_rows_total(self):
        return (EnvelopeLine.objects.filter(envelope__date=SABBATH)
                .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    def _ledger_total(self):
        return (Transaction.objects.filter(
                    date=SABBATH, channel=Transaction.Channel.ENVELOPE,
                    direction=Transaction.Direction.CREDIT)
                .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    def _fund_receipts(self, fund, start=None, end=MONTH_END):
        from reports.services import balances
        for row in balances.department_summary(start, end):
            if getattr(row.get("department", None), "id", None) == fund.id:
                return row["receipts"]
        raise WorkflowError(f"{fund.name} is not in the fund summary at all.")

    def _fund_summary_receipts_total(self, start=None, end=MONTH_END):
        from reports.services import balances
        return sum((r["receipts"] for r in balances.department_summary(start, end)),
                   Decimal(0))

    # ==================================================================
    # 1. the whole cycle
    # ==================================================================

    def test_a_sabbath_walks_from_the_plate_to_the_fund_balances(self):
        # 1. after the service the deacons count the cash box and two of them
        #    sign for it. Nothing has been keyed in yet, so the system expects
        #    nothing and the count stands alone.
        self.visit(self.cashier, "count_new", query=f"?date={SABBATH.isoformat()}")
        self.submit(self.cashier, "count_new", {
            "date": SABBATH.isoformat(),
            "d_1000": "11", "d_500": "4", "d_200": "3", "d_100": "1",
            "note": "Counted in the vestry after second service.",
            "w_name": ["Joseph Mwangi", "Ruth Chebet"],
            "w_role": ["Head deacon", "Deaconess"],
            "w_signed_0": "on", "w_signed_1": "on",
        })
        count = CountSession.objects.get(date=SABBATH)
        self.assertEqual(
            count.counted_total, CASH_IN_THE_PLATE,
            "the denomination breakdown did not add up to the cash counted")
        self.assertEqual(count.witnesses.count(), 2)

        # 2. the cashier opens the envelope sheet for that Sabbath and keys the
        #    envelopes in — contributor, receipt number, amounts across the fund
        #    columns. The grid auto-saves into a DRAFT batch as she types.
        self.visit(self.cashier, "envelope_ledger",
                   query=f"?date={SABBATH.isoformat()}")
        half = self._key_in(self.cashier, self._rows_payload(PLATE[:3]))
        batch_id = half["batch_id"]
        # ... she is interrupted, comes back, and finishes the sheet. The grid
        #     re-sends the WHOLE sheet, into the same draft.
        self._key_in(self.cashier, self._rows_payload(PLATE), batch_id=batch_id)

        batch = EnvelopeBatch.objects.get(pk=batch_id)
        self.assertEqual(batch.status, EnvelopeBatch.Status.DRAFT)
        self.assertEqual(batch.rows.count(), len(PLATE))
        self.assertEqual(batch.computed_total(), SABBATH_TOTAL)

        # 3. a DRAFT is a worksheet, not an entry. Not one shilling of it has
        #    reached a fund, the ledger, or the cash count's expectation.
        self.assertEqual(Envelope.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)
        self.assert_fund_balance(self.tithe, Decimal(0), as_of=MONTH_END)
        self.assert_fund_balance(self.offering, Decimal(0), as_of=MONTH_END)

        # 4. she submits it for review
        self.submit(self.cashier, "envelope_batch_submit", {}, args=[batch_id])
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.REVIEW)
        self.assertEqual(batch.submitted_by, self.assistant)

        # 5. ...and it STILL contributes nothing. "Submitted" is the state a
        #    church's money spends longest in, and a report that quietly
        #    included it would be indistinguishable from one that did not.
        self.assertEqual(Transaction.objects.count(), 0)
        self.assert_fund_balance(self.tithe, Decimal(0), as_of=MONTH_END)

        # 6. the treasurer reviews the sheet on screen and approves it
        self.visit(self.office, "envelope_batch_detail", args=[batch_id])
        self.submit(self.office, "envelope_batch_approve", {}, args=[batch_id])
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.APPROVED)
        self.assertEqual(batch.reviewed_by, self.treasurer)
        self.assertEqual(Transaction.objects.count(), 0,
                         "approval is a decision, not a posting")

        # 7. and posts it — the only action in the whole pipeline that writes
        #    to the accounts
        self._post(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.POSTED)
        self.assertEqual(batch.posted_by, self.treasurer)

        # 8. the envelopes now exist, one per row, each keeping its receipt
        #    number and carrying its whole gift
        self.assertEqual(Envelope.objects.count(), len(PLATE))
        for receipt, name, channel, split in PLATE:
            env = Envelope.objects.get(receipt_no=receipt)
            self.assertEqual(env.date, SABBATH)
            self.assertEqual(env.contributor_name, name.upper())
            self.assertEqual(env.channel, channel)
            self.assertEqual(env.member, self.members[name],
                             f"{receipt} lost the member it was keyed against")
            self.assertEqual(
                env.total, sum(Decimal(v) for v in split.values()),
                f"envelope {receipt} did not post its whole amount")
            self.assertEqual(
                env.lines.count(), len(split),
                f"envelope {receipt} posted {env.lines.count()} fund lines, "
                f"{len(split)} were keyed — this is the #63 shape: a line "
                f"dropped with the receipt number still consumed")

        # 9. every row of the batch knows which envelope it became. A row that
        #    posted nothing and said nothing is the silent drop.
        for row in batch.rows.all():
            self.assertIsNotNone(
                row.posted_envelope,
                f"row {row.line_no} ({row.contributor_name}) was in an "
                f"APPROVED batch that posted, and became no envelope")
            self.assertEqual(row.posted_envelope.total, row.computed_total)

        # 10. THE MONEY. The same Sabbath, read five ways.
        self._agree(
            "the plate, read five ways after posting",
            plate=SABBATH_TOTAL,
            batch_worksheet=batch.computed_total(),
            envelope_rows=self._envelope_rows_total(),
            ledger_transactions=self._ledger_total(),
            envelope_totals=Envelope.objects.filter(date=SABBATH)
                .aggregate(t=Sum("total"))["t"] or Decimal(0),
        )

        # 11. and each fund holds exactly its share of it
        self.assert_fund_balance(self.tithe, TITHE_TOTAL, as_of=MONTH_END)
        self.assert_fund_balance(self.offering, OFFERING_TOTAL, as_of=MONTH_END)
        self.assert_fund_balance(self.camp, CAMP_TOTAL, as_of=MONTH_END)

        # 12. ...and it is dated to the Sabbath it was given on, not to the day
        #     it was keyed or the day it was posted. An as-of read taken the day
        #     before must show none of it; taken on the Sabbath itself, all of
        #     it. "The right figure read as of the wrong date" is its own family
        #     of defect in this application and it never shows up in a total.
        self.assert_fund_balance(self.tithe, Decimal(0),
                                 as_of=SABBATH - dt.timedelta(days=1))
        self.assert_fund_balance(self.tithe, TITHE_TOTAL, as_of=SABBATH)

        # 13. the invariants: a workflow that produces the right fund balance
        #     while unbalancing the ledger has not worked.
        self.assert_books_balance("after posting a Sabbath's envelopes")
        self.assert_trial_balance_balances(MONTH_START, MONTH_END)

    # ==================================================================
    # 2. the seam the audit kept finding money at
    # ==================================================================

    def test_the_money_in_the_envelope_rows_equals_the_movement_in_the_funds(self):
        """Issue #63, stated as an invariant rather than as a fund-deactivation
        story: whatever the envelope lines say was given must be exactly what
        the funds received. A dropped line breaks this and nothing else.
        """
        batch = self._walk_to_approved()
        self._post(batch)

        per_fund = {
            "tithe": TITHE_TOTAL,
            "offering": OFFERING_TOTAL,
            "camp": CAMP_TOTAL,
        }
        for key, expected in per_fund.items():
            fund = self.funds[key]
            lines = (EnvelopeLine.objects.filter(envelope__date=SABBATH,
                                                 department=fund)
                     .aggregate(t=Sum("amount"))["t"] or Decimal(0))
            self._agree(
                f"{fund.name}: what the envelopes say vs what the fund received",
                keyed_on_the_sheet=expected,
                envelope_lines=lines,
                fund_summary_receipts=self._fund_receipts(fund),
            )

        # and nothing landed anywhere it was not keyed to
        self._agree(
            "the Sabbath total vs the sum of every fund's receipts",
            sabbath_total=SABBATH_TOTAL,
            all_fund_receipts=self._fund_summary_receipts_total(),
        )
        self.assert_books_balance("after checking line-to-fund agreement")

    def test_the_collections_summary_agrees_with_the_fund_summary(self):
        """Two reports, two entirely different assemblies of the same money —
        the Collections Summary aggregates confirmed credits by month, the fund
        summary aggregates receipts by department. This codebase's single most
        common defect is exactly this pair drifting apart.
        """
        batch = self._walk_to_approved()
        self._post(batch)

        # read the report the way a treasurer does: through its page
        page = self.visit(self.office, "report_collections_summary",
                          query=f"?year={SABBATH.year}")
        summary = page.context["d"]
        detail_page = self.visit(
            self.office, "report_collections_detail",
            query=f"?start={MONTH_START.isoformat()}"
                  f"&end={MONTH_END.isoformat()}")
        detail = detail_page.context["d"]

        year_start = dt.date(SABBATH.year, 1, 1)
        year_end = dt.date(SABBATH.year, 12, 31)
        self._agree(
            "collections for the year, against the funds that received them",
            sabbath_total=SABBATH_TOTAL,
            collections_summary=summary["tot_collections"],
            collections_detail=detail["tot_collections"],
            fund_summary_receipts=self._fund_summary_receipts_total(
                year_start, year_end),
        )

        # the trust/local split has to survive the same journey: Tithe and
        # Camp Meeting are the field's money, Combined Offering is the
        # church's, and the two reports slice that differently.
        self._agree(
            "trust-fund collections",
            summary_trust=summary["tot_trust"],
            detail_trust=detail["tot_trust"],
            trust_funds=TITHE_TOTAL + CAMP_TOTAL,
        )
        self._agree(
            "local-fund collections",
            summary_local=summary["tot_local"],
            detail_local=detail["tot_local"],
            local_funds=OFFERING_TOTAL,
        )
        self.assertEqual(
            detail["n_receipts"],
            EnvelopeLine.objects.filter(envelope__date=SABBATH).count(),
            "the detail report counted a different number of receipts than "
            "there are envelope fund lines")

    def test_the_cash_count_reconciles_once_the_sabbath_is_posted(self):
        """The count is taken before the sheet is keyed, so it starts out
        looking like a discrepancy. Once the Sabbath is posted the expected
        figure has to catch up — and it must expect the CASH only, not the
        envelope given by M-Pesa that was written on the same sheet. Counting
        that as cash is how a count becomes impossible to balance.
        """
        self.submit(self.cashier, "count_new", {
            "date": SABBATH.isoformat(),
            "d_1000": "11", "d_500": "4", "d_200": "3", "d_100": "1",
            "w_name": ["Joseph Mwangi"], "w_role": ["Head deacon"],
            "w_signed_0": "on",
        })
        count = CountSession.objects.get(date=SABBATH)
        self.assertEqual(count.expected_total, Decimal(0))
        self.assertTrue(count.has_discrepancy,
                        "nothing is keyed in yet, so the count cannot agree")

        batch = self._walk_to_approved()
        self._post(batch)

        # the treasurer reopens the count and saves it again, which is how the
        # expected figure is refreshed after the sheet goes in
        self.submit(self.office, "count_edit", {
            "date": SABBATH.isoformat(),
            "d_1000": "11", "d_500": "4", "d_200": "3", "d_100": "1",
            "w_name": ["Joseph Mwangi"], "w_role": ["Head deacon"],
            "w_signed_0": "on",
        }, args=[count.pk])
        count.refresh_from_db()

        self._agree(
            "the cash box against what the system says should be in it",
            counted_in_the_vestry=count.counted_total,
            expected_by_the_system=count.expected_total,
            cash_envelopes_only=CASH_IN_THE_PLATE,
        )
        self.assertFalse(
            count.has_discrepancy,
            f"the count is out by {count.discrepancy:,.2f} — the most likely "
            f"cause is the M-Pesa envelope on the sheet being counted as cash")
        self.assert_books_balance("after reconciling the cash count")

    # ==================================================================
    # 3. what must NOT happen
    # ==================================================================

    def _close_camp_meeting(self):
        """The Monday after the Sabbath, a treasurer closes the Camp Meeting
        fund — through the screen that does it. The close gate only allows a
        fund at a zero balance, and this one IS at zero: the Sabbath's cash is
        counted, keyed, approved and sitting in an unposted batch, which is
        precisely the window #63 lives in."""
        self.submit(self.office, "department_close",
                    {"note": "Camp meeting is over for the year."},
                    args=[self.camp.pk])
        self.camp.refresh_from_db()
        self.assertEqual(
            self.camp.status, Department.Status.CLOSED,
            "the fund did not close — the rest of this walk proves nothing")
        self.assertFalse(self.camp.active)

    def test_a_fund_closed_between_approval_and_posting_still_gets_its_money(self):
        """Issue #63's own story, walked through the screens rather than
        asserted on the service.

        Two contributors put money in the Camp Meeting column on Sabbath. The
        batch is approved. Before anyone clicks Post, the fund is closed —
        legitimately, through the register, because at that moment it holds
        nothing. Then the Sabbath is posted. Closing a fund means "no NEW money
        against this"; it must never mean money already counted, receipted and
        approved evaporates on its way to the ledger, with the receipt numbers
        consumed and nothing saying so.
        """
        batch = self._walk_to_approved()
        self._close_camp_meeting()
        self._post(batch)
        batch.refresh_from_db()
        self.assertEqual(
            batch.status, EnvelopeBatch.Status.POSTED,
            "an approved Sabbath refused to post because a fund it references "
            "closed after it was approved — the cash is already in the safe")

        # every envelope kept every line it was keyed with. The #63 shape is an
        # envelope that posts SHORT: receipt consumed, one line missing.
        for receipt, _name, _channel, split in PLATE:
            env = Envelope.objects.get(receipt_no=receipt)
            self.assertEqual(
                env.lines.count(), len(split),
                f"envelope {receipt} posted {env.lines.count()} of "
                f"{len(split)} keyed lines after a fund was closed")
            self.assertEqual(env.total, sum(Decimal(v) for v in split.values()))
        self.assertFalse(
            Envelope.objects.filter(total=0).exists(),
            "a zero-total envelope against a real receipt number is the exact "
            "artefact #63 left behind")

        # and the closed fund's own money is all there, in the envelope rows
        # and in the ledger alike
        camp_lines = (EnvelopeLine.objects.filter(envelope__date=SABBATH,
                                                  department=self.camp)
                      .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        camp_ledger = (Transaction.objects.filter(
                            date=SABBATH, department=self.camp,
                            channel=Transaction.Channel.ENVELOPE)
                       .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        self._agree(
            "the closed fund's share of the Sabbath",
            keyed_on_the_sheet=CAMP_TOTAL,
            envelope_lines=camp_lines,
            ledger_transactions=camp_ledger,
        )
        self._agree(
            "the whole Sabbath, after a fund closed mid-flight",
            plate=SABBATH_TOTAL,
            envelope_rows=self._envelope_rows_total(),
            ledger_transactions=self._ledger_total(),
        )
        self.assert_books_balance("after posting into a fund closed mid-flight")
        self.assert_trial_balance_balances(MONTH_START, MONTH_END)

    def test_a_sabbath_posted_into_a_closed_fund_still_shows_in_the_fund_summary(self):
        """DEFECT (found by this suite).

        `reports.services.balances._department_summary_impl` builds its rows
        from ``Department.objects.filter(active=True)``. A fund that has been
        closed is `active=False`, so it and every shilling it holds drop out of
        the master fund summary entirely — no row, no balance, not even a zero.

        That is normally harmless, because `CloseAccountView` refuses to close a
        fund with a balance, so a closed fund holds nothing. The #63 fix opens
        the one door through which it can come to hold something: an approved
        batch is deliberately allowed to post into a fund closed after approval,
        which is right — the money was given while the fund was open. The two
        rules meet at a seam nobody walks: the Sabbath posts in full, the ledger
        balances, the Collections Summary shows all 23,700 — and the fund
        summary shows 22,950, because Camp Meeting's 750 is simply not on it.
        Money that exists in one report and not in another is the exact defect
        class this suite was written for, and 'a fund missing from a report'
        reads identically to 'a fund that received nothing'.

        The close screen itself promises the opposite, in its own success
        message: "It stays in historical reports but won't accept new
        transactions."

        Fix: `_department_summary_impl` should include inactive departments that
        have any movement or balance in the period (or all of them, and let the
        template hide empty closed rows) rather than filtering on `active`.
        """
        batch = self._walk_to_approved()
        self._close_camp_meeting()
        self._post(batch)

        # the money itself is fine — this test is only about where it is shown
        self.assertEqual(self._ledger_total(), SABBATH_TOTAL)

        page = self.visit(self.office, "report_collections_summary",
                          query=f"?year={SABBATH.year}")
        year_start = dt.date(SABBATH.year, 1, 1)
        year_end = dt.date(SABBATH.year, 12, 31)
        self._agree(
            "the same Sabbath, read from the two reports that must agree",
            collections_summary=page.context["d"]["tot_collections"],
            fund_summary_receipts=self._fund_summary_receipts_total(
                year_start, year_end),
        )

    def test_a_batch_cannot_be_posted_twice(self):
        """The second click of Post. The money must not double, and no receipt
        number may be consumed a second time."""
        batch = self._walk_to_approved()
        self._post(batch)

        envelopes_after_first = Envelope.objects.count()
        first_reading = self._ledger_total()

        # ...the treasurer's browser hiccups and Post is clicked again
        again = self._post(batch, allow_refusal=True)
        self.assertLess(again.status_code, 500)

        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.POSTED)
        self.assertEqual(Envelope.objects.count(), envelopes_after_first,
                         "posting twice created a second set of envelopes")
        self._agree(
            "the Sabbath after Post was clicked twice",
            after_the_first_post=first_reading,
            after_the_second=self._ledger_total(),
            what_was_given=SABBATH_TOTAL,
        )
        self.assert_fund_balance(self.tithe, TITHE_TOTAL, as_of=MONTH_END)
        self.assert_books_balance("after a double Post")

    def test_a_draft_and_a_submitted_batch_contribute_exactly_nothing(self):
        """Two Sabbaths' worth of worksheets sitting in the queue, and the
        accounts must read as though neither existed. This is the assertion a
        per-step test cannot make: absence looks like correctness."""
        # one posted Sabbath, so the funds hold a real figure to be wrong about
        posted = self._walk_to_approved()
        self._post(posted)
        baseline_tithe = TITHE_TOTAL
        self.assert_fund_balance(self.tithe, baseline_tithe, as_of=MONTH_END)

        next_sabbath = SABBATH + dt.timedelta(days=7)
        later = [("R-4201", "Grace Wanjiru", "CASH", {"tithe": "9999"})]

        # a draft, abandoned mid-entry
        draft_id = self._key_in(
            self.cashier,
            self._rows_payload(later, first_line=1),
            date=next_sabbath)["batch_id"]

        # and a second sheet, keyed and submitted, waiting for a treasurer
        pending = [("R-4202", "Peter Otieno", "CASH", {"offering": "7777"})]
        pending_id = self._key_in(
            self.cashier, self._rows_payload(pending),
            date=next_sabbath)["batch_id"]
        self.submit(self.cashier, "envelope_batch_submit", {}, args=[pending_id])

        self.assertEqual(
            EnvelopeBatch.objects.get(pk=draft_id).status,
            EnvelopeBatch.Status.DRAFT)
        self.assertEqual(
            EnvelopeBatch.objects.get(pk=pending_id).status,
            EnvelopeBatch.Status.REVIEW)

        # neither of them is money
        self.assert_fund_balance(self.tithe, baseline_tithe, as_of=MONTH_END)
        self.assert_fund_balance(self.offering, OFFERING_TOTAL, as_of=MONTH_END)
        self.assertEqual(Envelope.objects.filter(date=next_sabbath).count(), 0)
        self._agree(
            "the accounts, with two unposted worksheets in the queue",
            what_was_actually_posted=SABBATH_TOTAL,
            all_fund_receipts=self._fund_summary_receipts_total(
                MONTH_START, MONTH_END + dt.timedelta(days=14)),
        )

        # but they are not lost either — the review queue is where they live,
        # and a batch nobody can find is the other half of the same failure
        queue = self.visit(self.office, "envelope_batch_list")
        listed = {b.id for b in queue.context["batches"]}
        self.assertIn(draft_id, listed)
        self.assertIn(pending_id, listed)
        self.assert_books_balance("with unposted batches in the queue")

    def test_a_receipt_number_already_posted_cannot_be_claimed_again(self):
        """A receipt number is a physical stub in a book. Once it has been
        posted, a second sheet reusing it must be stopped at the gate rather
        than discovered later as two gifts against one stub."""
        posted = self._walk_to_approved()
        self._post(posted)

        clash = [("R-4102", "Ruth Chebet", "CASH", {"offering": "600"})]
        clash_id = self._key_in(
            self.cashier, self._rows_payload(clash),
            date=SABBATH + dt.timedelta(days=7),
            allow_row_errors=True)["batch_id"]

        self.submit(self.cashier, "envelope_batch_submit", {}, args=[clash_id])
        clashing = EnvelopeBatch.objects.get(pk=clash_id)
        self.assertEqual(
            clashing.status, EnvelopeBatch.Status.DRAFT,
            "a batch reusing a posted receipt number was allowed through")
        self.assertEqual(
            Envelope.objects.filter(receipt_no="R-4102").count(), 1)
        self.assert_fund_balance(self.offering, OFFERING_TOTAL, as_of=MONTH_END)
        self.assert_books_balance("after a receipt-number clash was refused")

    def test_an_auditor_can_read_the_batch_and_cannot_pass_or_post_it(self):
        """Segregation walked rather than asserted on a mixin. A hidden button
        is not a permission (#137d) — so this posts to the URLs directly."""
        batch = self._walk_to_approved()

        reading_room = self.acting_as(self.auditor)
        self.visit(reading_room, "envelope_batch_list")
        self.visit(reading_room, "envelope_batch_detail", args=[batch.pk])

        reading_room.post(reverse("envelope_batch_post", args=[batch.pk]))
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.APPROVED,
                         "a read-only auditor posted a Sabbath's giving")
        self.assertEqual(Envelope.objects.count(), 0)
        self.assert_fund_balance(self.tithe, Decimal(0), as_of=MONTH_END)

    def test_a_treasurer_cannot_pass_her_own_sheet_but_a_second_one_can(self):
        """Maker-checker, walked. The officer who keys a Sabbath in here is a
        full treasurer — she has every permission the approve and post URLs
        demand, so nothing about her role turns her away. What must turn her
        away is the church's own rule that a second pair of eyes signs off,
        and it has to hold at the view, not only in the template that hides
        the button (#137d)."""
        keyer = self.make_user("wf_treasurer_2", "Treasurer")
        her_desk = self.acting_as(keyer)

        batch_id = self._key_in(her_desk, self._rows_payload())["batch_id"]
        self.submit(her_desk, "envelope_batch_submit", {}, args=[batch_id])

        # she tries to pass her own sheet
        self.submit(her_desk, "envelope_batch_approve", {}, args=[batch_id],
                    allow_form_errors=True)
        batch = EnvelopeBatch.objects.get(pk=batch_id)
        self.assertEqual(
            batch.status, EnvelopeBatch.Status.REVIEW,
            "the treasurer who keyed the sheet in approved it herself")
        self.assertEqual(Transaction.objects.count(), 0)

        # the OTHER treasurer can, and does
        self.submit(self.office, "envelope_batch_approve", {}, args=[batch_id])
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.APPROVED)

        # ...and she still may not put her own sheet into the accounts
        self.submit(her_desk, "envelope_batch_post", {}, args=[batch_id],
                    allow_form_errors=True)
        batch.refresh_from_db()
        self.assertEqual(batch.status, EnvelopeBatch.Status.APPROVED)
        self.assertEqual(Envelope.objects.count(), 0)
        self.assert_fund_balance(self.tithe, Decimal(0), as_of=MONTH_END)

        # the second treasurer posts it, and only then is it money
        self.submit(self.office, "envelope_batch_post", {}, args=[batch_id])
        self.assert_fund_balance(self.tithe, TITHE_TOTAL, as_of=MONTH_END)
        self.assert_books_balance("after a properly checked batch was posted")

    # ==================================================================
    # 4. where the workflow ends
    # ==================================================================

    def test_every_page_the_sabbath_ends_on_actually_opens(self):
        """A Sabbath that is counted, keyed, approved and posted and then
        cannot be looked at has not been dealt with — and this is the specific
        failure this application shipped five times."""
        self.submit(self.cashier, "count_new", {
            "date": SABBATH.isoformat(), "d_1000": "11", "d_500": "4",
            "d_200": "3", "d_100": "1",
            "w_name": ["Joseph Mwangi"], "w_role": ["Head deacon"],
            "w_signed_0": "on"})
        count = CountSession.objects.get(date=SABBATH)

        batch = self._walk_to_approved()
        self._post(batch)

        self.visit(self.office, "count_list")
        self.visit(self.office, "count_detail", args=[count.pk])
        self.visit(self.office, "envelope_batch_list")
        self.visit(self.office, "envelope_batch_detail", args=[batch.pk])
        self.visit(self.office, "envelope_sabbath_entries",
                   args=[SABBATH.isoformat()])
        self.visit(self.office, "envelope_list",
                   query=f"?month={SABBATH.year}-{SABBATH.month:02d}")

        entries = self.visit(self.office, "envelope_sabbath_entries",
                             args=[SABBATH.isoformat()])
        body = entries.content.decode()
        for receipt, _name, _channel, _split in PLATE:
            self.assertIn(receipt, body,
                          f"{receipt} was posted and is not on its own "
                          f"Sabbath's page")

        for env in Envelope.objects.all():
            self.visit(self.office, "envelope_detail", args=[env.pk])
            self.visit(self.office, "envelope_receipt", args=[env.pk])

    def test_posting_lands_the_treasurer_on_the_sabbath_just_posted(self):
        """DEFECT (found by this suite).

        `EnvelopeBatchPostView` finishes by redirecting to
        ``envelope_list?date=<sabbath>``. `EnvelopeListView` has no ``date``
        parameter at all — it reads ``month`` (``YYYY-MM``) and otherwise falls
        back to *today's* calendar month. So a treasurer who posts a batch for
        any Sabbath outside the current month is dropped on a month overview
        that does not contain the Sabbath they just posted, with a success
        message about envelopes they cannot see. Correcting a receipt entered
        wrongly means navigating back by hand.

        Fix: redirect with ``?month=YYYY-MM`` (or straight to
        ``envelope_sabbath_entries`` for the Sabbath, which is the page that
        actually lists the receipts).
        """
        if dt.date.today().strftime("%Y-%m") == SABBATH.strftime("%Y-%m"):
            self.skipTest(
                "today falls inside the workflow's own month, so the ignored "
                "?date= parameter cannot be told apart from a working one")

        batch = self._walk_to_approved()
        landing = self._post(batch)

        self.assertEqual(
            landing.context["month_value"], SABBATH.strftime("%Y-%m"),
            f"posting redirected to {landing.redirect_chain[-1][0]!r}, and the "
            f"page came back showing {landing.context['month_value']} instead "
            f"of the Sabbath's own month")
