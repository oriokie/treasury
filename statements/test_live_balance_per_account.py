"""The live bank balance must belong to the account it is quoted under.

`live_balance_asof` narrowed its search by `account.number` — a field
`BankAccount` has never had; the number on it is `account_number`. So the
`getattr` handed back "" on every call, the filter it guarded never once ran,
and the function returned the freshest balance anywhere on the feed whatever
account it had been asked about.

That figure is not decorative. It feeds `reports.services.balances.bank_position`
(the official Bank Position) and `statements.views.suggested_bank_balance`, which
pre-fills the closing balance of a NEW reconciliation — so a church whose
Development account is busier than its Current account reconciled Current
against Development's balance, and hunted for a difference that was never in the
books.

The other half of the same fault: the rule for deciding which events belong to
an account was written out longhand in `statements/webhook.py` and nowhere else,
so the reporting side had nothing to read and drifted. Both sides now go through
`register.acct_no_q` / `register.bank_account_for_acct_no`, and the last test
here holds them to the same answer.
"""
import datetime as dt
import json
from decimal import Decimal

from django.test import Client, TestCase

from core.models import SiteConfig
from statements.models import BankAccount, BankEvent
from statements.services import register as register_svc


def _event(acct_no, day, balance, ref):
    return BankEvent.objects.create(
        cbs_transaction_id=ref, acct_no=acct_no, amount=Decimal("1"),
        event_type="CREDIT", booked_balance=balance, cleared_balance=balance,
        balance_at=day, payload="{}", status=BankEvent.Status.PROCESSED)


class TwoAccountsAreNotOneTests(TestCase):
    """The reproduction: Current holds 100,000 and was told so first;
    Development holds 9,999,999 and was told so later. Ask about Current."""

    def setUp(self):
        self.current = BankAccount.objects.create(
            name="Current", account_number="01134248358600", is_default=True)
        self.development = BankAccount.objects.create(
            name="Development", account_number="01128158400600")
        _event("01134248358600", dt.date(2026, 7, 1), Decimal("100000"), "CB_CUR_1")
        _event("01128158400600", dt.date(2026, 7, 30), Decimal("9999999"), "CB_DEV_1")

    def test_the_account_asked_about_is_the_account_answered_for(self):
        """THE bug: Current was reported as holding Development's 9,999,999."""
        result = register_svc.live_balance_asof(dt.date(2026, 7, 31), self.current)
        self.assertEqual(result["balance"], Decimal("100000"))
        self.assertEqual(result["as_at"], dt.date(2026, 7, 1))

    def test_the_other_account_still_answers_for_itself(self):
        result = register_svc.live_balance_asof(dt.date(2026, 7, 31),
                                                self.development)
        self.assertEqual(result["balance"], Decimal("9999999"))

    def test_an_account_the_feed_has_never_mentioned_reports_nothing(self):
        """Something sane, not a neighbour's figure. Nothing is the honest
        answer, and the Bank Position falls back to the register for it — a
        wrong balance under the right account's name is the whole fault."""
        savings = BankAccount.objects.create(name="Savings",
                                             account_number="01199999999600")
        result = register_svc.live_balance_asof(dt.date(2026, 7, 31), savings)
        self.assertIsNone(result["balance"])
        self.assertIn("Savings", result["reason"])

    def test_the_feed_may_drop_the_leading_zero_and_still_be_recognised(self):
        """The bank and Settings are two people writing the same number. The
        last six digits agreeing is the test — the same one the webhook has
        always used to route an incoming payment to an account."""
        _event("1134248358600", dt.date(2026, 7, 29), Decimal("123456"), "CB_CUR_2")
        result = register_svc.live_balance_asof(dt.date(2026, 7, 31), self.current)
        self.assertEqual(result["balance"], Decimal("123456"))

    def test_an_account_with_no_number_beside_others_says_so(self):
        """With several accounts configured and no number to tell them apart,
        there is no answer to give — so say which setting is missing rather than
        quoting whichever balance happened to arrive last."""
        unnumbered = BankAccount.objects.create(name="Building fund")
        result = register_svc.live_balance_asof(dt.date(2026, 7, 31), unnumbered)
        self.assertIsNone(result["balance"])
        self.assertIn("account number", result["reason"].lower())


class TheSingleAccountChurchIsUnaffectedTests(TestCase):
    """The common case, and the reason the filter must not simply be switched
    on and left strict: most churches have one account, and many have never
    typed its number into Settings at all."""

    def setUp(self):
        self.only = BankAccount.objects.create(name="Main", is_default=True)

    def test_a_balance_is_still_given_when_the_number_was_never_filled_in(self):
        _event("01134248358600", dt.date(2026, 7, 30), Decimal("250000"), "CB_1")
        result = register_svc.live_balance_asof(dt.date(2026, 7, 31), self.only)
        self.assertEqual(result["balance"], Decimal("250000"))

    def test_an_archived_neighbour_does_not_make_this_the_multi_account_case(self):
        """Closing an account must not cost the survivor its live balance.

        "Is there another account to confuse this with" is a question about
        accounts still in use, not about rows in the table — an archived one is
        never going to receive the feed's next balance. Counting rows instead
        would have quietly turned a church that closed an old account, and never
        typed a number into the one it kept, from "here is your balance" into
        "no live balance", for a reason no one could have guessed from the page.
        """
        BankAccount.objects.create(name="Old current", account_number="01100000000600",
                                   active=False)
        _event("01134248358600", dt.date(2026, 7, 30), Decimal("250000"), "CB_3")
        result = register_svc.live_balance_asof(dt.date(2026, 7, 31), self.only)
        self.assertEqual(result["balance"], Decimal("250000"))

    def test_a_balance_is_still_given_when_the_feed_quotes_a_number_we_do_not_hold(self):
        """There is nowhere else the money could be. Reporting "no live balance"
        because two strings disagree would be a worse answer, not a safer one."""
        self.only.account_number = "01134248358600"
        self.only.save(update_fields=["account_number"])
        _event("CO-OP/1134248358600/KES", dt.date(2026, 7, 30),
               Decimal("250000"), "CB_2")
        result = register_svc.live_balance_asof(dt.date(2026, 7, 31), self.only)
        self.assertEqual(result["balance"], Decimal("250000"))


class TheWebhookAndTheReportAgreeTests(TestCase):
    """One rule, read by both ends. The webhook decides which account a payment
    landed in; the report decides which balances are that account's. They used to
    decide separately, and only one of them was right."""

    PAYLOAD = {
        "AcctNo": "01128158400600", "Amount": "100.0",
        "BookedBalance": "1146822.03", "ClearedBalance": "1146822.03",
        "Currency": "KES", "EventType": "CREDIT",
        "Narration": "UGUDP0NH6R~441211#offering~254700374441~MPESAC2B~C MEYO",
        "PaymentRef": "30072026_209567121", "PostingDate": "2026-07-30",
        "ValueDate": "2026-07-30", "TransactionDate": "2026-07-30",
        "TransactionId": "CB0764882_30072026_9",
    }

    def setUp(self):
        cfg = SiteConfig.get()
        cfg.bank_feed_enabled = True
        cfg.bank_feed_auth_mode = SiteConfig.BankFeedAuth.TOKEN
        cfg.bank_feed_token = "test-secret"
        cfg.save()
        self.current = BankAccount.objects.create(
            name="Current", account_number="01134248358600", is_default=True)
        self.development = BankAccount.objects.create(
            name="Development", account_number="01128158400600")
        response = Client().post(
            "/api/bank/cbs-events/", data=json.dumps(self.PAYLOAD),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-secret")
        self.assertEqual(response.status_code, 200)

    def test_the_payment_was_routed_to_the_development_account(self):
        from giving.models import Transaction
        txn = Transaction.objects.get(core_ref="CB0764882_30072026_9")
        self.assertEqual(txn.bank_account_id, self.development.id)

    def test_and_its_balance_is_the_development_accounts_balance(self):
        result = register_svc.live_balance_asof(dt.date(2026, 7, 30),
                                                self.development)
        self.assertEqual(result["balance"], Decimal("1146822.03"))

    def test_and_is_not_quoted_for_the_current_account(self):
        result = register_svc.live_balance_asof(dt.date(2026, 7, 30), self.current)
        self.assertIsNone(result["balance"])
