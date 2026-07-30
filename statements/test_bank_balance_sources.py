"""Where the bank balance comes from, and how old it is.

Three faults, all of them the same shape: a figure the bank had already given us
being ignored in favour of one that did not answer the question asked.

1. **The bank states its balance on every event it pushes.** `BookedBalance` and
   `ClearedBalance` arrive with each transaction on the CBS feed. The webhook
   read the amount, the type, the currency, the reference and the dates — and
   not the balances. They went into the raw payload blob and were read by
   nothing. So the one live figure the church had was arriving continuously and
   being thrown away, while the treasurer's report had none.

2. **The report showed the wrong date's balance.** It took the closing balance of
   whichever statement had been imported most recently, whatever date the report
   was run for — so a report for 30 June carried September's bank balance beside
   June's movements, and the difference between them meant nothing.

3. **The page would not open without a statement import.** A church running the
   feed and importing nothing saw "import a bank statement to enable this check"
   while the bank told it the balance several times a day.

The rule now: both the register and the live feed are the bank's own word, so
whichever is nearer the date being asked about wins, and the page says which it
used and how old it is. A stale figure is still the bank's last word — but a
treasurer reading a month-end report needs to know it is three weeks old rather
than being shown it as the closing position.
"""
import datetime as dt
import json
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core import roles
from core.models import SiteConfig
from statements.models import BankAccount, BankEvent, StatementLine
from statements.services import register as register_svc

PAYLOAD = {
    "AcctNo": "01128158400600", "Amount": "100.0",
    "BookedBalance": "1146822.03", "ClearedBalance": "1146822.03",
    "Currency": "KES", "EventType": "CREDIT",
    "Narration": "UGUDP0NH6R~441211#CMbooklet~254700374441~MPESAC2B_400222~CATHERINE MEYO",
    "PaymentRef": "30072026_209567121", "PostingDate": "2026-07-30",
    "ValueDate": "2026-07-30", "TransactionDate": "2026-07-30T16:30:05",
    "TransactionId": "CB0764882_30072026_2",
}


class TheFeedsBalanceIsKeptTests(TestCase):
    """What the bank sends must be stored, not left inside a blob."""

    def setUp(self):
        cfg = SiteConfig.get()
        cfg.bank_feed_enabled = True
        cfg.bank_feed_auth_mode = SiteConfig.BankFeedAuth.TOKEN
        cfg.bank_feed_token = "test-secret"
        cfg.save()
        self.client = Client()

    def _post(self, **overrides):
        payload = dict(PAYLOAD, **overrides)
        return self.client.post(
            "/api/bank/cbs-events/", data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-secret")

    def test_a_real_payload_is_accepted(self):
        self.assertEqual(self._post().status_code, 200)

    def test_the_booked_balance_is_stored(self):
        self._post()
        event = BankEvent.objects.get(cbs_transaction_id=PAYLOAD["TransactionId"])
        self.assertEqual(event.booked_balance, Decimal("1146822.03"))

    def test_the_cleared_balance_is_stored(self):
        self._post()
        event = BankEvent.objects.get(cbs_transaction_id=PAYLOAD["TransactionId"])
        self.assertEqual(event.cleared_balance, Decimal("1146822.03"))

    def test_the_balance_is_dated(self):
        """Undated, a balance cannot answer "as at when"."""
        self._post()
        event = BankEvent.objects.get(cbs_transaction_id=PAYLOAD["TransactionId"])
        self.assertEqual(event.balance_at, dt.date(2026, 7, 30))

    def test_an_event_without_balances_is_still_accepted(self):
        """Not every bank sends them; the transaction still matters."""
        payload = {k: v for k, v in PAYLOAD.items()
                   if k not in ("BookedBalance", "ClearedBalance")}
        payload["TransactionId"] = "CB_NO_BALANCE_1"
        response = self.client.post(
            "/api/bank/cbs-events/", data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-secret")
        self.assertEqual(response.status_code, 200)
        event = BankEvent.objects.get(cbs_transaction_id="CB_NO_BALANCE_1")
        self.assertIsNone(event.booked_balance)


class WhichBalanceAnswersTheDateTests(TestCase):

    def setUp(self):
        self.account = (BankAccount.objects.filter(is_default=True).first()
                        or BankAccount.objects.create(name="Main", is_default=True))
        for i, (day, balance) in enumerate((
                (dt.date(2026, 6, 30), Decimal("250000")),
                (dt.date(2026, 7, 20), Decimal("310000")))):
            StatementLine.objects.create(
                account=self.account, date=day, dedup_key=f"bal{i}",
                credit=Decimal("0"), debit=Decimal("0"), bank_balance=balance)

    def _live(self, day, balance):
        return BankEvent.objects.create(
            cbs_transaction_id=f"CB_{day}", acct_no="", amount=Decimal("1"),
            event_type="CREDIT", booked_balance=balance,
            cleared_balance=balance, balance_at=day, payload="{}",
            status=BankEvent.Status.PROCESSED)

    # -- the register --------------------------------------------------------

    def test_the_register_answers_the_date_asked(self):
        result = register_svc.balance_asof(dt.date(2026, 6, 30), self.account)
        self.assertEqual(result["balance"], Decimal("250000"))
        self.assertEqual(result["as_at"], dt.date(2026, 6, 30))

    def test_a_date_between_lines_uses_the_last_one_before_it(self):
        result = register_svc.balance_asof(dt.date(2026, 7, 10), self.account)
        self.assertEqual(result["balance"], Decimal("250000"))
        self.assertEqual(result["as_at"], dt.date(2026, 6, 30))

    def test_the_age_of_the_figure_is_reported(self):
        """A balance ten days old is still the bank's word — but say so."""
        result = register_svc.balance_asof(dt.date(2026, 7, 10), self.account)
        self.assertEqual(result["stale_days"], 10)

    def test_a_date_before_the_register_starts_is_reported_not_invented(self):
        """A fabricated balance would reconcile against itself."""
        result = register_svc.balance_asof(dt.date(2026, 1, 1), self.account)
        self.assertIsNone(result["balance"])
        self.assertIn("no balance", result["reason"].lower())

    # -- the live feed -------------------------------------------------------

    def test_the_live_feed_answers_the_date_asked(self):
        self._live(dt.date(2026, 7, 30), Decimal("1146822.03"))
        result = register_svc.live_balance_asof(dt.date(2026, 7, 30))
        self.assertEqual(result["balance"], Decimal("1146822.03"))

    def test_the_live_feed_does_not_answer_for_a_later_balance(self):
        """Asking about June must not return July's figure."""
        self._live(dt.date(2026, 7, 30), Decimal("1146822.03"))
        result = register_svc.live_balance_asof(dt.date(2026, 6, 30))
        self.assertIsNone(result["balance"])

    # -- which one wins ------------------------------------------------------

    def _position(self, on):
        from reports.services.balances import bank_position
        return bank_position(as_of=on)

    def test_the_register_answers_where_the_feed_is_silent(self):
        position = self._position(dt.date(2026, 6, 30))
        self.assertEqual(position["balance_source"], "register")
        self.assertEqual(position["statement_balance"], Decimal("250000"))

    def test_a_fresher_live_figure_beats_a_stale_register(self):
        """Both are the bank's word; the nearer one answers the question."""
        self._live(dt.date(2026, 7, 30), Decimal("1146822.03"))
        position = self._position(dt.date(2026, 7, 30))
        self.assertEqual(position["balance_source"], "live")
        self.assertEqual(position["statement_balance"], Decimal("1146822.03"))

    def test_a_fresher_register_beats_an_older_live_figure(self):
        self._live(dt.date(2026, 6, 1), Decimal("90000"))
        position = self._position(dt.date(2026, 7, 25))
        self.assertEqual(position["balance_source"], "register")
        self.assertEqual(position["statement_balance"], Decimal("310000"))

    def test_the_report_no_longer_shows_the_latest_import_for_every_date(self):
        """The original fault: June's report carried the newest balance."""
        june = self._position(dt.date(2026, 6, 30))
        july = self._position(dt.date(2026, 7, 25))
        self.assertNotEqual(june["statement_balance"], july["statement_balance"])


class TheReportOpensWithoutAStatementImportTests(TestCase):
    """A church running the feed and importing nothing can still reconcile."""

    def setUp(self):
        self.user = User.objects.create_user("tess-bank", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.user)

    def _page(self):
        response = self.client.get("/reports/bank-position/")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_without_any_balance_it_says_how_to_get_one(self):
        body = self._page()
        self.assertIn("bank feed", body)

    def test_a_live_balance_alone_is_enough_to_show_the_check(self):
        BankEvent.objects.create(
            cbs_transaction_id="CB_UI", acct_no="", amount=Decimal("1"),
            event_type="CREDIT", booked_balance=Decimal("1146822.03"),
            cleared_balance=Decimal("1100000.00"), balance_at=dt.date.today(),
            payload="{}", status=BankEvent.Status.PROCESSED)
        body = self._page()
        self.assertIn("1,146,822.03", body)
        self.assertIn("live feed", body)

    def test_the_cleared_figure_is_shown_when_it_differs(self):
        """Booked and cleared answer different questions."""
        BankEvent.objects.create(
            cbs_transaction_id="CB_UI2", acct_no="", amount=Decimal("1"),
            event_type="CREDIT", booked_balance=Decimal("1146822.03"),
            cleared_balance=Decimal("1100000.00"), balance_at=dt.date.today(),
            payload="{}", status=BankEvent.Status.PROCESSED)
        self.assertIn("cleared and available", self._page())
