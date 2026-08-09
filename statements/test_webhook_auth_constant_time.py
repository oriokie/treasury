"""The bank webhook's BASIC credentials must be compared in constant time.

The TOKEN branch of `CbsEventWebhookView._authenticated` has always used
`hmac.compare_digest`, with a comment saying so. The BASIC branch a few lines
above used a plain `==`, and BASIC is the documented default — so the mode
nearly every church runs was the one comparing its password byte by byte,
stopping at the first difference. Against an endpoint that is deliberately
reachable without a session, and that answers every attempt, the difference in
how long the 401 takes to come back is enough to recover a secret one character
at a time.

There is no honest timing assertion to write here — a clock on a test runner
proves nothing — so the guard is on the mechanism: the comparison must go
through `compare_digest`, and it must keep working for the credentials a real
church might actually set.
"""
import base64
import hmac
import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from core.models import SiteConfig


def _basic(user, pwd):
    raw = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"HTTP_AUTHORIZATION": f"Basic {raw}"}


PAYLOAD = {
    "AcctNo": "01134248358600", "Amount": "10.0", "Currency": "KES",
    "EventType": "CREDIT", "Narration": "UAUTH11111~441211#tithe~254700~C2B~A",
    "PaymentRef": "01082026_1", "PostingDate": "2026-08-01",
    "ValueDate": "2026-08-01", "TransactionDate": "2026-08-01",
    "TransactionId": "CB_AUTH_1",
}


@override_settings(AXES_ENABLED=False)
class BasicAuthIsComparedInConstantTimeTests(TestCase):

    def setUp(self):
        cfg = SiteConfig.get()
        cfg.bank_feed_enabled = True
        cfg.bank_feed_auth_mode = SiteConfig.BankFeedAuth.BASIC
        cfg.bank_feed_username = "coopbank"
        cfg.bank_feed_password = "s3cret-passphrase"
        cfg.save()
        self.client = Client()

    def _post(self, txn_id="CB_AUTH_1", **headers):
        return self.client.post(
            "/api/bank/cbs-events/", data=json.dumps(dict(PAYLOAD,
                                                          TransactionId=txn_id)),
            content_type="application/json", **headers)

    def _compare_digest_calls(self, **headers):
        """Every pair `hmac.compare_digest` was asked about during one request."""
        seen = []
        real = hmac.compare_digest

        def _spy(a, b):
            seen.append((a, b))
            return real(a, b)

        with patch("statements.webhook.hmac.compare_digest", _spy):
            self._post(**headers)
        return seen

    def test_the_password_goes_through_compare_digest(self):
        """The regression itself. With `==` the secret is never handed to
        `compare_digest` at all, and the comparison short-circuits on the first
        wrong byte."""
        pairs = self._compare_digest_calls(**_basic("coopbank", "s3cret-passphrase"))
        self.assertTrue(
            any(b"s3cret-passphrase" in pair for pair in pairs),
            "the configured password was never compared in constant time")

    def test_the_username_goes_through_compare_digest_too(self):
        """A username is not a secret, but the time taken to reject one says
        whether it was the right username — which narrows the guessing."""
        pairs = self._compare_digest_calls(**_basic("coopbank", "s3cret-passphrase"))
        self.assertTrue(any(b"coopbank" in pair for pair in pairs))

    def test_a_wrong_username_still_has_the_password_compared(self):
        """`and` would stop at the username and answer faster, which is itself
        the leak: it tells a caller the username was wrong."""
        pairs = self._compare_digest_calls(**_basic("nobody", "s3cret-passphrase"))
        self.assertTrue(any(b"s3cret-passphrase" in pair for pair in pairs))

    # -- and it still authenticates the way it always did ---------------------

    def test_the_right_credentials_are_still_accepted(self):
        response = self._post(**_basic("coopbank", "s3cret-passphrase"))
        self.assertEqual(response.status_code, 200)

    def test_a_wrong_password_is_still_rejected(self):
        self.assertEqual(
            self._post(**_basic("coopbank", "s3cret-passphras")).status_code, 401)

    def test_a_wrong_username_is_still_rejected(self):
        self.assertEqual(
            self._post(**_basic("nobody", "s3cret-passphrase")).status_code, 401)

    def test_a_password_with_an_accent_in_it_does_not_break_the_endpoint(self):
        """`compare_digest` refuses a str holding anything outside ASCII, so
        comparing the text rather than its UTF-8 bytes would raise TypeError
        inside the auth check — a 500 on the one endpoint the bank calls, for a
        church that simply typed a password with an accent in it."""
        cfg = SiteConfig.get()
        cfg.bank_feed_password = "pässwörd-ke"
        cfg.save()
        self.assertEqual(
            self._post(**_basic("coopbank", "pässwörd-ke")).status_code, 200)
        self.assertEqual(
            self._post(txn_id="CB_AUTH_2",
                       **_basic("coopbank", "passwörd-ke")).status_code, 401)
