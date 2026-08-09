"""Money arriving at the bank, becoming income, and being reconciled.

The process, end to end: the bank tells the church about a credit (live, over
the CBS webhook, or later as a statement file); the credit has no reference the
system recognises, so it lands in the review queue owing nothing to any fund; a
treasurer allocates it; and at month end a reconciliation worksheet is prepared
for the statement date, reconciling items are added, and it balances.

Each of those steps is unit-tested. What no unit test can do is walk the SEAM
between them, and the seams are where this area has failed: v3.41.0-v3.41.3 were
three consecutive releases in which the worksheet and the dashboard, which are
the same figure, stopped being the same figure. The invariants walked here are
the three the audit named:

* money at the bank before the period end but not yet receipted is counted
  EXACTLY ONCE for that date — in a fund, or in suspense, never both and never
  neither, and the total does not move when the receipting finally happens;
* a worksheet prepared for a past date gives the SAME answer when it is opened
  again later — nothing done afterwards may move a settled figure;
* the worksheet's cash-book balance IS the dashboard's closing balance for that
  date, and the cash position, and the fund summary. Not close to. The same.
"""
import base64
import datetime as dt
import json
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.models import SiteConfig
from departments.models import current_cash_position
from giving.models import Transaction
from reports.services import balances
from statements.models import BankAccount, BankEvent, BankReconciliation

from .base import PERIOD_END, PERIOD_START, BusinessWorkflowTest, WorkflowError

#: The church's account at the bank, as the bank writes it on every event.
ACCOUNT_NO = "01134248358600"
FEED_USER = "coopbank"
FEED_PASSWORD = "cbs-feed-secret-1"

#: The month being closed. PERIOD_END is 31 July 2026.
MONTH_END = PERIOD_END


class BankIntakeToReconciliation(BusinessWorkflowTest):
    """One church, one bank account, one month of giving, one worksheet."""

    def setUp(self):
        super().setUp()
        self.office = self.acting_as(self.treasurer)

        # Background configuration, not a step of this workflow: the bank's
        # credentials are keyed into Settings once, by an administrator, long
        # before any money arrives. (Driving them through the settings page
        # would mean posting one enormous ModelForm covering every option in
        # the system — that tests the settings page, not the bank feed.)
        cfg = SiteConfig.get()
        cfg.bank_feed_enabled = True
        cfg.bank_feed_auth_mode = SiteConfig.BankFeedAuth.BASIC
        cfg.bank_feed_username = FEED_USER
        cfg.bank_feed_password = FEED_PASSWORD
        cfg.save()

        # 1. the treasurer registers the church's bank account, on the page
        #    that registers one. Everything downstream — which account an event
        #    belongs to, which register a balance is read from — hangs off this.
        self.submit(self.office, "bank_accounts", {
            "name": "Main Current", "bank_name": "Co-operative Bank",
            "account_number": ACCOUNT_NO, "kind": "CURRENT", "is_default": "on"})
        self.account = BankAccount.objects.get(account_number=ACCOUNT_NO)

        # 2. the first three weeks of July, walked the same way the rest of the
        #    workflow is: the bank calls, the credits queue, the treasurer
        #    allocates them. This is the populated ledger every later assertion
        #    is read against — a fund with real gifts, real members and real
        #    bank events in it, not an empty one (documented failure #125).
        self.bank_says(when=dt.date(2026, 7, 3), amount="120000",
                       narration="UER2Q5NF2W~441211#buildingwork~254790301470"
                                 "~MPESAC2B~JOHN MWANGI",
                       txn_id="CB0100001_03072026")
        self.bank_says(when=dt.date(2026, 7, 5), amount="80000",
                       narration="UC7QM8P5XY~441211#firstfruit~254790301471"
                                 "~MPESAC2B~MARY WANJIKU",
                       txn_id="CB0100002_05072026")
        self.bank_says(when=dt.date(2026, 7, 8), amount="15000",
                       narration="UD8RN9Q6ZA~441211#buildingwork~254790301472"
                                 "~MPESAC2B~PETER OTIENO",
                       txn_id="CB0100003_08072026")

        self.allocate_queue_to(self.local_fund,
                               references=["buildingwork"])       # 135,000
        self.allocate_queue_to(self.trust_fund,
                               references=["firstfruit"])         # 80,000
        self.early_july = Decimal("215000")

    # -- driving the bank -----------------------------------------------------

    def bank_says(self, *, when, amount, narration, txn_id, event_type="CREDIT",
                  balance=None, auth=True, expect=200):
        """The bank's Core Banking System calls the church, over HTTP.

        A private helper rather than the harness's `submit` because this caller
        is not a browser and not a person: it sends a raw JSON body with an
        Authorization header and no session, and the reply it needs is the
        bank's own {"MessageCode": ...} envelope rather than a rendered page.
        Everything else about it is the harness's rule — a real request to the
        real URL, and a reply that is checked rather than assumed.
        """
        payload = {
            "AcctNo": ACCOUNT_NO, "Amount": str(amount), "Currency": "KES",
            "EventType": event_type, "Narration": narration,
            "PaymentRef": txn_id, "TransactionId": txn_id,
            "TransactionDate": f"{when.isoformat()}+03:00",
            "PostingDate": f"{when.isoformat()}+03:00",
            "ValueDate": f"{when.isoformat()}+03:00",
        }
        if balance is not None:
            payload["BookedBalance"] = str(balance)
            payload["ClearedBalance"] = str(balance)
        headers = {}
        if auth:
            raw = base64.b64encode(
                f"{FEED_USER}:{FEED_PASSWORD}".encode()).decode()
            headers["HTTP_AUTHORIZATION"] = f"Basic {raw}"
        response = self.client.post(
            reverse("cbs_webhook"), data=json.dumps(payload),
            content_type="application/json", **headers)
        if response.status_code != expect:
            raise WorkflowError(
                f"The bank posted {event_type} {amount} on {when} and the church "
                f"replied {response.status_code} (expected {expect}): "
                f"{response.content[:200]!r}. A non-2XX makes the bank re-deliver.")
        return response

    # -- driving the treasurer ------------------------------------------------

    def queued(self):
        """Exactly what the review queue page is showing, in its own order."""
        page = self.visit(self.office, "queue")
        return list(page.context["items"])

    def allocate_queue_to(self, fund, references):
        """Allocate every queued credit carrying one of these references.

        The treasurer's own two routes, both used: one item at a time through
        the claim-and-resolve form, and the rest in one action through bulk
        allocate — because a queue is cleared both ways and the two write the
        row differently.
        """
        wanted = [t for t in self.queued() if t.reference in references]
        if not wanted:
            raise WorkflowError(
                f"Nothing in the review queue carries {references} — the credits "
                f"the bank sent never reached the treasurer.")
        first, rest = wanted[0], wanted[1:]
        self.submit(self.office, "queue_claim", {"department": fund.id},
                    args=[first.pk])
        if rest:
            self.submit(self.office, "queue_bulk_allocate", {
                "department": fund.id, "txn": [t.pk for t in rest]})
        for t in wanted:
            t.refresh_from_db()
            if t.department_id != fund.id:
                raise WorkflowError(
                    f"{t.amount} from {t.payer_name} was allocated to "
                    f"{t.department} instead of {fund.name} — the queue page "
                    f"reported success and moved nothing.")
        return wanted

    def start_worksheet(self, *, statement_date, bank_balance):
        """Prepare a reconciliation for a date, on the page that prepares one."""
        before = set(BankReconciliation.objects.values_list("pk", flat=True))
        self.submit(self.office, "reconciliation_new", {
            "statement_date": statement_date.isoformat(),
            "bank_balance": str(bank_balance),
            "book_balance": "",          # left blank: the app fills it in
            "notes": "Month end"})
        new = BankReconciliation.objects.exclude(pk__in=before).first()
        if new is None:
            raise WorkflowError(
                "The 'new reconciliation' page accepted the form and created no "
                "worksheet.")
        return new

    def reopen(self, rec):
        """Open an existing worksheet as the treasurer, and re-read it.

        Opening is not a passive act here — the detail view re-syncs the
        auto-managed reconciling items for anyone who can enter data — which is
        exactly why a workflow test has to do it through the page rather than
        refresh the model.
        """
        page = self.visit(self.office, "reconciliation_detail", args=[rec.pk])
        rec.refresh_from_db()
        return page

    # -- the figures the invariants are about ---------------------------------

    @staticmethod
    def money(value):
        """A figure at the scale money is kept at, ready to be compared.

        `assert_agree` compares `str(Decimal(v))`, which is exponent-sensitive:
        `Decimal("255000.00")` and `Decimal(255000)` are the same money and
        different strings. A total that came back from a database aggregate
        therefore "disagreed" with the identical total summed in Python. Every
        figure in this file is shillings and cents, so pinning the scale before
        comparing loses nothing real and stops the helper crying wolf.
        """
        return Decimal(value).quantize(Decimal("0.01"))

    def dashboard_closing(self, on):
        """The closing balance the dashboard prints for a period ending `on`."""
        page = self.visit(self.office, "dashboard",
                          query=f"?start={PERIOD_START.isoformat()}&end={on.isoformat()}")
        return page.context["totals"]["closing"]

    def bank_credits_to(self, on):
        """Every shilling the bank actually put into the account by `on`, net of
        anything it took out again. The outside figure the books are measured
        against — it knows nothing about funds, queues or receipting."""
        rows = Transaction.objects.filter(
            channel=Transaction.Channel.BANK, date__lte=on,
            is_reversal=False, is_reversed=False)
        total = Decimal(0)
        for t in rows:
            total += (t.amount if t.direction == Transaction.Direction.CREDIT
                      else -t.amount)
        return total

    # =========================================================================
    # the workflow
    # =========================================================================

    def test_a_credit_arrives_at_the_bank_and_ends_up_reconciled(self):
        """The spine: bank → queue → fund → worksheet → balanced."""
        # 1. an unrecognised credit lands on the last day of the month. Nothing
        #    in the system knows what "harvestgift" is for, so no rule can
        #    allocate it.
        self.bank_says(when=MONTH_END, amount="40000",
                       narration="UF1SP2R7BC~441211#harvestgift~254790301473"
                                 "~MPESAC2B~ESTHER NJERI",
                       txn_id="CB0100004_31072026", balance="255000")
        gift = Transaction.objects.get(core_ref="CB0100004_31072026")
        self.assertEqual(gift.department_id, None,
                         "an unrecognised credit must not guess at a fund")
        self.assertEqual(gift.allocation_status, Transaction.Status.REVIEW)

        # 2. it is on the queue page the treasurer actually works from
        self.assertIn(gift.pk, [t.pk for t in self.queued()],
                      "the credit the bank sent never reached the review queue")

        # 3. and the bank feed log — the page ops look at when a church rings up
        #    to say the money has not arrived — shows the event as processed
        event = BankEvent.objects.get(cbs_transaction_id="CB0100004_31072026")
        self.assertEqual(event.status, BankEvent.Status.PROCESSED)
        self.visit(self.office, "bank_feed_log")

        # 4. the treasurer allocates it
        self.allocate_queue_to(self.local_fund, references=["harvestgift"])

        # 5. the money is now IN the fund, to the shilling
        self.assert_fund_balance(self.local_fund, Decimal("175000"), MONTH_END)
        self.assert_fund_balance(self.trust_fund, Decimal("80000"), MONTH_END)

        # 6. the worksheet for 31 July, prepared on the page that prepares one.
        #    The bank says 255,000; so do the books; so it balances at nil.
        rec = self.start_worksheet(statement_date=MONTH_END,
                                   bank_balance=Decimal("255000"))
        self.assertEqual(rec.book_balance, Decimal("255000"))
        self.assertEqual(rec.difference, Decimal("0"),
                         f"the worksheet is out by {rec.difference}")
        self.assertTrue(rec.is_reconciled)

        # 7. and the books themselves still hold together
        self.assert_books_balance("after a month of bank intake")
        self.assert_trial_balance_balances()

    def test_the_treasurer_adds_a_reconciling_item_and_the_sheet_balances(self):
        """A bank charge the books have never heard of.

        The bank takes 500 for ledger fees. It is real money gone from the
        account and nothing in the cash book knows about it, so the two sides
        cannot agree until the treasurer enters it as a reconciling item — which
        is the whole point of the worksheet and the one step of it that is
        typed rather than computed.
        """
        self.bank_says(when=dt.date(2026, 7, 30), amount="500",
                       narration="LEDGER FEES JULY", event_type="DEBIT",
                       txn_id="CB0100009_30072026")

        # the bank now holds 214,500 while the cash book still says 215,000
        self.assert_agree(
            "the cash book, before the charge is explained",
            cash_book=self.money(current_cash_position(MONTH_END)),
            early_july_giving=self.money(self.early_july))

        rec = self.start_worksheet(statement_date=MONTH_END,
                                   bank_balance=Decimal("214500"))
        self.assertEqual(rec.difference, Decimal("-500"),
                         "a charge the books do not carry must show as a gap")

        # 1. the treasurer explains the gap on the worksheet page
        self.submit(self.office, "reconciliation_detail", {
            "action": "add_item", "kind": "BANK_CHARGE",
            "description": "Ledger fees, 30 July — not yet in the cash book",
            "amount": "500", "effect": "ADD"}, args=[rec.pk])
        rec.refresh_from_db()

        # 2. and now it balances
        self.assertEqual(rec.difference, Decimal("0"),
                         f"the worksheet is still out by {rec.difference}")
        self.assertEqual(rec.items.filter(auto=False).count(), 1,
                         "the typed item did not stick")

        # 3. re-opening it does not disturb the item somebody typed
        self.reopen(rec)
        self.assertEqual(rec.difference, Decimal("0"))
        self.assert_books_balance("after reconciling a bank charge")

    def test_money_banked_before_month_end_and_receipted_after_is_counted_once(self):
        """The invariant the audit put first: exactly once, and never moving.

        40,000 arrives on 31 July with a reference nobody recognises, and is
        allocated in August. For a 31 JULY reading, the total the church held at
        the bank cannot change because of something a treasurer did in August —
        only WHERE it is counted may change: suspense before the allocation, the
        fund after it. Both, or neither, is the defect, and both have shipped.
        """
        self.bank_says(when=MONTH_END, amount="40000",
                       narration="UF1SP2R7BC~441211#harvestgift~254790301473"
                                 "~MPESAC2B~ESTHER NJERI",
                       txn_id="CB0100004_31072026", balance="255000")

        # --- before the treasurer gets to it ---------------------------------
        book_before = current_cash_position(MONTH_END)
        suspense_before = balances.pending_receipts_total(MONTH_END)
        self.assertEqual(suspense_before, Decimal("40000"),
                         "money at the bank and in no fund must sit in suspense")
        self.assert_fund_balance(self.local_fund, Decimal("135000"), MONTH_END)

        # --- the treasurer allocates it (in August, as really happens) -------
        self.allocate_queue_to(self.local_fund, references=["harvestgift"])

        book_after = current_cash_position(MONTH_END)
        suspense_after = balances.pending_receipts_total(MONTH_END)
        self.assertEqual(suspense_after, Decimal("0"),
                         "once it is in a fund it must leave suspense — carrying "
                         "it in both is how one gift gets reported as two")
        self.assert_fund_balance(self.local_fund, Decimal("175000"), MONTH_END)

        # --- and the 31 July position is the same money either way -----------
        self.assert_agree(
            "the 31 July bank position, before and after the receipting",
            bank_statement=self.money(self.bank_credits_to(MONTH_END)),
            book_plus_suspense_before=self.money(book_before + suspense_before),
            book_plus_suspense_after=self.money(book_after + suspense_after))

        self.assert_books_balance("after a late allocation")

    def test_the_worksheet_cash_book_is_the_dashboard_closing_balance(self):
        """Three pages, one figure. This is what v3.41.2 broke.

        The reconciliation rebuilt its cash-book balance from history and so
        dropped everything keyed in after the statement date, and became the
        only page in the system disagreeing with the dashboard. Walked here
        through the pages themselves rather than the service they share.
        """
        self.bank_says(when=MONTH_END, amount="40000",
                       narration="UF1SP2R7BC~441211#harvestgift~254790301473"
                                 "~MPESAC2B~ESTHER NJERI",
                       txn_id="CB0100004_31072026", balance="255000")
        self.allocate_queue_to(self.local_fund, references=["harvestgift"])

        rec = self.start_worksheet(statement_date=MONTH_END,
                                   bank_balance=Decimal("255000"))
        page = self.reopen(rec)

        self.assert_agree(
            "the cash-book balance as at 31 July, read four ways",
            worksheet_stored=self.money(rec.book_balance),
            worksheet_page_suggestion=self.money(page.context["suggested_book"]),
            dashboard_closing=self.money(self.dashboard_closing(MONTH_END)),
            cash_position=self.money(current_cash_position(MONTH_END)))

        # and the worksheet's own explanation of that figure ties to it
        diag = page.context["diag"]
        self.assert_agree(
            "the worksheet's diagnostic against its own cash-book balance",
            diagnostic_book=self.money(diag["book"]),
            worksheet_stored=self.money(rec.book_balance))
        self.assert_agree(
            "opening + income - expenses + transfers, against the same book",
            components=self.money(diag["opening"] + diag["income"]
                                  - diag["expenses"] + diag["transfers"]),
            diagnostic_book=self.money(diag["book"]))

    def test_a_worksheet_settled_in_july_still_balances_when_opened_in_august(self):
        """DEFECT — opening a settled worksheet later un-settles it.

        The story: 40,000 lands on 31 July with a reference nobody recognises.
        On 31 July the treasurer prepares the worksheet. The books do not have
        the 40,000 (it is in no fund), the bank does, and the app puts the
        difference on the sheet itself as the managed item "Receipts pending
        allocation" — so the worksheet balances at nil and July is closed.

        In August the treasurer allocates the gift, then opens July's worksheet
        again to file it. The detail view re-syncs its managed items and deletes
        that 40,000 line, because nothing is pending any more — but the
        cash-book balance it is compared against is the figure STORED in July,
        and nothing recomputes it. One side of the comparison moved and the
        other did not, so a reconciliation that balanced now shows a 40,000
        difference and `is_reconciled` goes from True to False.

        Both readings are individually defensible; the pair is not. Either the
        stored book balance must be refreshed alongside the managed items (the
        two are coupled by construction — see `_pending_at_the_date`'s own
        docstring: "either way the two sides add to the same money, so a
        reconciliation for 31 July balances whether it is prepared on the day,
        the next morning, or in November") or a settled worksheet must not be
        re-synced at all. As it stands, merely LOOKING at July's worksheet in
        August breaks it — and with `auto_lock_on_reconciliation` on, July was
        locked on the strength of a figure the app then withdrew.
        """
        self.bank_says(when=MONTH_END, amount="40000",
                       narration="UF1SP2R7BC~441211#harvestgift~254790301473"
                                 "~MPESAC2B~ESTHER NJERI",
                       txn_id="CB0100004_31072026", balance="255000")

        # 1. 31 July: prepared while the gift is still unallocated, and settled
        rec = self.start_worksheet(statement_date=MONTH_END,
                                   bank_balance=Decimal("255000"))
        self.assertEqual(rec.difference, Decimal("0"),
                         "the sheet must balance on the day, with the pending "
                         "receipt carried as a reconciling item")
        self.assertTrue(rec.is_reconciled)
        settled_book = rec.book_balance

        # 2. August: the treasurer allocates the gift
        self.allocate_queue_to(self.local_fund, references=["harvestgift"])

        # 3. and opens July's worksheet again to file it
        self.reopen(rec)

        self.assertEqual(
            rec.difference, Decimal("0"),
            f"July's reconciliation balanced at nil and now differs by "
            f"{rec.difference}: the managed 'receipts pending allocation' item "
            f"was removed on re-opening while the cash-book balance stayed at "
            f"{settled_book}.")
        self.assertTrue(rec.is_reconciled)

    def test_the_balance_the_app_offers_is_the_one_that_makes_the_sheet_balance(self):
        """The figure nobody should have to type.

        The bank states the account's balance on every event it pushes, so the
        new-reconciliation page offers it rather than making a treasurer copy it
        off a statement — where a transposed digit sends them hunting for a
        difference that was never in the books. The seam worth walking is that
        the offered figure is not merely present but RIGHT: a worksheet built on
        it, with nothing typed at all, must reconcile to nil.
        """
        self.bank_says(when=MONTH_END, amount="40000",
                       narration="UF1SP2R7BC~441211#harvestgift~254790301473"
                                 "~MPESAC2B~ESTHER NJERI",
                       txn_id="CB0100004_31072026", balance="255000")
        self.allocate_queue_to(self.local_fund, references=["harvestgift"])

        # 1. the page pre-fills the bank balance for the date being reconciled
        page = self.visit(self.office, "reconciliation_new",
                          query=f"?statement_date={MONTH_END.isoformat()}")
        self.assertTrue(page.context["balance_suggested"],
                        "the bank pushed a balance on 31 July and the "
                        "new-reconciliation page offered nothing")
        offered = page.context["form"].initial["bank_balance"]

        # 2. so does the endpoint the date picker calls when the date changes —
        #    a suggestion that does not follow the date is a trap, not a help
        api = self.office.get(reverse("reconciliation_balance")
                              + f"?date={MONTH_END.isoformat()}")
        self.assertTrue(api.json()["ok"], api.json())
        self.assert_agree(
            "the balance offered on the page and by the date picker",
            page_initial=self.money(offered),
            date_picker=self.money(api.json()["balance"]),
            bank_last_said=self.money("255000"))

        # 3. and a worksheet accepting the offer reconciles to nil
        rec = self.start_worksheet(statement_date=MONTH_END, bank_balance=offered)
        self.assertEqual(rec.difference, Decimal("0"),
                         f"the balance the app itself offered leaves the "
                         f"worksheet out by {rec.difference}")

    def test_a_statement_file_walks_the_same_road_as_the_live_feed(self):
        """The other way money gets in. Same queue, same allocation, same books.

        A church that misses a week of the live feed uploads the bank's own file
        instead, and the two routes must land in the same place — that is the
        promise `statements.services.ingest`'s docstring makes.
        """
        csv = (
            "Transaction Date,Narration,Credit Amount,Core Ref\n"
            "2026-07-21,UG2TQ3S8CD~441211#harvestgift~254790301474"
            "~MPESAC2B~SAMUEL KIPROTICH,30000,CB0200001_21072026\n"
            "2026-07-22,UH3UR4T9DE~441211#harvestgift~254790301475"
            "~MPESAC2B~RUTH CHEBET,10000,CB0200002_22072026\n"
        ).encode("utf-8")

        # 1. the treasurer uploads the statement
        upload = SimpleUploadedFile("july.csv", csv, content_type="text/csv")
        self.submit(self.office, "statement_upload",
                    {"file": upload, "bank_account": self.account.id})

        from statements.models import StatementImport
        imp = StatementImport.objects.order_by("-id").first()
        self.assertEqual(imp.status, StatementImport.Status.DONE,
                         f"the import failed: {imp.error_detail}")
        self.assertEqual(imp.queued_for_review, 2,
                         "both unrecognised credits should be queued")

        # 2. the import's own page opens and says what happened
        self.visit(self.office, "statement_list")
        self.visit(self.office, "statement_detail", args=[imp.pk])

        # 3. they are queued, in no fund, and carried as suspense
        self.assert_fund_balance(self.local_fund, Decimal("135000"), MONTH_END)
        self.assertEqual(balances.pending_receipts_total(MONTH_END),
                         Decimal("40000"))

        # 4. the treasurer clears them
        self.allocate_queue_to(self.local_fund, references=["harvestgift"])
        self.assert_fund_balance(self.local_fund, Decimal("175000"), MONTH_END)

        # 5. and a worksheet for the month balances against the bank's 255,000
        rec = self.start_worksheet(statement_date=MONTH_END,
                                   bank_balance=Decimal("255000"))
        self.assertEqual(rec.difference, Decimal("0"),
                         f"the worksheet is out by {rec.difference}")
        self.assert_books_balance("after a statement file import")
        self.assert_trial_balance_balances()

    def test_the_bank_cannot_write_to_the_books_without_credentials(self):
        """The one endpoint the outside world posts money to.

        Not a security unit test — a workflow one. If an unauthenticated caller
        can bank money, every figure downstream in this file is somebody else's
        to set; and if a re-delivery creates a second copy, the month is
        overstated by the amount of the bank's own retry.
        """
        before = Transaction.objects.count()
        self.bank_says(when=MONTH_END, amount="999000",
                       narration="UZ9ZZ9Z9ZZ~441211#harvestgift~254790301479"
                                 "~MPESAC2B~NOBODY",
                       txn_id="CB0666666_31072026", auth=False, expect=401)
        self.assertEqual(Transaction.objects.count(), before,
                         "an unauthenticated caller banked money")

        # the bank re-delivers whenever it is unsure; the second copy must not
        # become a second gift
        self.bank_says(when=MONTH_END, amount="40000",
                       narration="UF1SP2R7BC~441211#harvestgift~254790301473"
                                 "~MPESAC2B~ESTHER NJERI",
                       txn_id="CB0100004_31072026")
        self.bank_says(when=MONTH_END, amount="40000",
                       narration="UF1SP2R7BC~441211#harvestgift~254790301473"
                                 "~MPESAC2B~ESTHER NJERI",
                       txn_id="CB0100004_31072026")
        self.assertEqual(
            Transaction.objects.filter(amount=Decimal("40000")).count(), 1,
            "a re-delivered event was banked twice")
        self.assert_agree(
            "one credit, delivered twice",
            bank_statement=self.money(self.bank_credits_to(MONTH_END)),
            expected=self.money(self.early_july + Decimal("40000")))

    def test_every_page_this_workflow_passes_through_actually_opens(self):
        """Where the workflow ends, and the specific failure this suite exists
        for (#121, #126): a screen that is built, wired and unreachable."""
        self.bank_says(when=MONTH_END, amount="40000",
                       narration="UF1SP2R7BC~441211#harvestgift~254790301473"
                                 "~MPESAC2B~ESTHER NJERI",
                       txn_id="CB0100004_31072026", balance="255000")
        self.allocate_queue_to(self.local_fund, references=["harvestgift"])
        rec = self.start_worksheet(statement_date=MONTH_END,
                                   bank_balance=Decimal("255000"))

        for page in ("statement_list", "statement_upload", "bank_accounts",
                     "bank_feed_log", "queue", "reconciliation_list",
                     "reconciliation_new", "bank_register",
                     "bank_register_exceptions", "auto_reconcile"):
            self.visit(self.office, page)
        self.visit(self.office, "reconciliation_detail", args=[rec.pk])

        # the register page for the account the money actually arrived in
        self.visit(self.office, "bank_register", query=f"?account={self.account.pk}")

    def test_an_auditor_can_read_the_worksheet_and_cannot_change_it(self):
        """Segregation at the seam. The detail view is read-gated, so an auditor
        gets in — and every write on it is a POST the template merely hides."""
        rec = self.start_worksheet(statement_date=MONTH_END,
                                   bank_balance=self.early_july)
        self.assertEqual(rec.difference, Decimal("0"))

        reading_room = self.acting_as(self.auditor)
        self.visit(reading_room, "reconciliation_detail", args=[rec.pk])
        reading_room.post(reverse("reconciliation_detail", args=[rec.pk]), {
            "action": "set_book", "book_balance": "1"})
        rec.refresh_from_db()
        self.assertEqual(rec.book_balance, self.early_july,
                         "a read-only auditor overwrote the cash-book balance")
