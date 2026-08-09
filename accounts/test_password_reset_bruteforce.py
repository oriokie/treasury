"""Guessing limits on the self-service SMS password-reset code.

The reset flow had a rate limit on how many codes were *issued* (three per
account per fifteen minutes, so nobody's phone can be bombed) and none at all on
how many guesses an issued code had to survive. Those are different questions
and only the first was being asked. Once a single code existed — and anyone can
cause one to exist, the request form is public and deliberately anonymous —
``SelfPasswordResetVerifyView`` would check any number of POSTed codes against
it for the whole ten minutes it lived. Six digits is 1,000,000 possibilities;
django-axes does not help here because axes only wraps ``authenticate()`` and
this view never calls it. That is a complete account takeover without ever
seeing the SMS.

The fix mirrors the two-factor login gate next door, which had already reasoned
this through for a 6-digit code (see ``TwoFactor.verify_code`` and
``TwoFactorVerifyView``'s session cap): a durable per-code counter in the
database, and a per-session counter that abandons the pending reset. Both read
one number, ``PasswordResetCode.MAX_VERIFY_ATTEMPTS``, so they cannot drift
apart.

The two layers are not redundant. The session counter is held in state the
attacker owns, so on its own it is only as strong as their willingness to clear
a cookie; the row counter is the one that actually binds, and the tests below
prove it by deliberately handing a fresh session to an attacker who already
burned the code.
"""
import datetime as dt

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import PasswordResetCode, UserProfile
from accounts.password_reset import PENDING_RESET_USER
from core.models import SiteConfig

MAX = PasswordResetCode.MAX_VERIFY_ATTEMPTS
NEW_PASSWORD = "BrandNewSecure99!"


def _sms_configured_user(username="bruteforce_target", phone="254712345678"):
    """Same fixture shape as accounts/test_self_password_reset.py: an account
    with a phone on file and SMS switched on, so the flow picks the SMS
    channel."""
    user = User.objects.create_user(username, password="OldPassword1234!")
    profile = UserProfile.for_user(user)
    profile.phone = phone
    profile.save()
    cfg = SiteConfig.get()
    cfg.sms_enabled = True
    cfg.sms_api_key = "testkey"
    cfg.sms_partner_id = "testpartner"
    cfg.sms_shortcode = "TEST"
    cfg.save()
    return user


def _start_flow(user):
    """Drive step 1 for real (so the session carries the pending user), then
    read the code the way the SMS recipient would."""
    client = Client()
    client.post("/accounts/forgot-password/", {"username": user.username})
    _, raw_code = PasswordResetCode.issue(user)
    return client, raw_code


def _guess(client, code, password=NEW_PASSWORD):
    return client.post("/accounts/forgot-password/verify/",
                       {"code": code, "new_password": password,
                        "confirm_password": password})


class CodeAttemptCapTests(TestCase):
    """The durable half: the limit lives on the code row, not on the session."""

    def setUp(self):
        self.user = User.objects.create_user("codecapuser", password="x")

    def test_a_code_stops_answering_after_the_attempt_cap(self):
        """The invariant that was missing entirely: a code is allowed a small,
        fixed number of wrong answers and then it is dead — including to the
        person holding the right answer, because by then we can no longer tell
        them apart from whoever was guessing."""
        _, raw_code = PasswordResetCode.issue(self.user)
        for _ in range(MAX):
            self.assertIsNone(PasswordResetCode.verify(self.user, "000000"))
        self.assertIsNone(
            PasswordResetCode.verify(self.user, raw_code),
            "the correct code still worked after the attempt cap was spent — "
            "the code can still be brute-forced")

    def test_mistyping_still_leaves_a_usable_number_of_tries(self):
        """The cap has to be survivable by a real person reading digits off a
        phone screen: wrong up to the last try, right on it."""
        _, raw_code = PasswordResetCode.issue(self.user)
        for _ in range(MAX - 1):
            self.assertIsNone(PasswordResetCode.verify(self.user, "000000"))
        self.assertIsNotNone(
            PasswordResetCode.verify(self.user, raw_code),
            f"a legitimate user got fewer than {MAX} tries")

    def test_a_correct_code_does_not_spend_a_try(self):
        """Only wrong answers count. The view checks the code before it checks
        that the two password boxes match, so counting correct answers too would
        let somebody fumble the confirmation field a few times and lose a code
        they had typed perfectly."""
        _, raw_code = PasswordResetCode.issue(self.user)
        for _ in range(MAX + 3):
            self.assertIsNotNone(PasswordResetCode.verify(self.user, raw_code))
        self.assertEqual(PasswordResetCode.remaining_attempts(self.user), MAX)

    def test_a_blank_code_does_not_spend_a_try(self):
        """An empty box is a submitted form, not a guess — mirrors
        ``TwoFactor.verify_code``, which also refuses an empty token before it
        touches the counter."""
        PasswordResetCode.issue(self.user)
        for _ in range(MAX + 3):
            self.assertIsNone(PasswordResetCode.verify(self.user, ""))
        self.assertEqual(PasswordResetCode.remaining_attempts(self.user), MAX)

    def test_remaining_attempts_counts_down_and_floors_at_zero(self):
        PasswordResetCode.issue(self.user)
        for spent in range(1, MAX + 1):
            PasswordResetCode.verify(self.user, "000000")
            self.assertEqual(PasswordResetCode.remaining_attempts(self.user),
                             MAX - spent)
        PasswordResetCode.verify(self.user, "000000")
        self.assertEqual(PasswordResetCode.remaining_attempts(self.user), 0)

    def test_remaining_attempts_is_zero_when_there_is_no_live_code(self):
        self.assertEqual(PasswordResetCode.remaining_attempts(self.user), 0)
        obj, _ = PasswordResetCode.issue(self.user)
        obj.expires_at = timezone.now() - dt.timedelta(minutes=1)
        obj.save(update_fields=["expires_at"])
        self.assertEqual(PasswordResetCode.remaining_attempts(self.user), 0)

    def test_a_fresh_code_starts_with_a_full_allowance(self):
        """Requesting a new code is the documented way back after burning one,
        so it must genuinely reset the counter rather than inherit it."""
        PasswordResetCode.issue(self.user)
        for _ in range(MAX):
            PasswordResetCode.verify(self.user, "000000")
        _, raw_code = PasswordResetCode.issue(self.user)
        self.assertEqual(PasswordResetCode.remaining_attempts(self.user), MAX)
        self.assertIsNotNone(PasswordResetCode.verify(self.user, raw_code))


class VerifyViewBruteForceTests(TestCase):
    """The same limit, seen from the outside — this is the surface an attacker
    actually has, and it is the one that was wide open."""

    def test_the_view_stops_accepting_guesses_and_drops_the_pending_reset(self):
        user = _sms_configured_user("viewcapuser")
        client, raw_code = _start_flow(user)
        for _ in range(MAX):
            _guess(client, "000000")
        response = _guess(client, raw_code)
        user.refresh_from_db()
        self.assertFalse(
            user.check_password(NEW_PASSWORD),
            "the password was reset with a code that had already been guessed "
            "at more times than the limit allows")
        self.assertEqual(response.status_code, 302,
                         "the exhausted flow kept re-rendering the code form "
                         "instead of sending the user back to request a new code")
        self.assertNotIn(PENDING_RESET_USER, client.session,
                         "the pending reset was left open after the cap was hit")

    def test_a_fresh_session_cannot_resurrect_a_burned_code(self):
        """The bypass a session-only counter would have: the attacker owns their
        cookie jar, so they simply get a new one and carry on. Only the counter
        stored beside the code itself can refuse this.

        The session is primed directly rather than by re-requesting a code,
        because re-requesting issues a *new* code and would test the wrong
        thing — the point here is the old, still-unexpired code."""
        user = _sms_configured_user("freshsessionuser")
        attacker, raw_code = _start_flow(user)
        for _ in range(MAX):
            _guess(attacker, "000000")

        second_jar = Client()
        session = second_jar.session
        session[PENDING_RESET_USER] = user.pk
        session.save()

        _guess(second_jar, raw_code)
        user.refresh_from_db()
        self.assertFalse(
            user.check_password(NEW_PASSWORD),
            "clearing cookies handed the attacker a fresh allowance against a "
            "code they had already exhausted")

    def test_the_legitimate_user_still_gets_through_after_a_typo(self):
        user = _sms_configured_user("typouser")
        client, raw_code = _start_flow(user)
        _guess(client, "000000")
        _guess(client, "111111")
        _guess(client, raw_code)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD),
                        "a user who mistyped twice was locked out of their own "
                        "reset")

    def test_a_mismatched_password_confirmation_does_not_burn_the_code(self):
        """A correct code with two different passwords typed in is a slip, not
        an attack, and must not eat the allowance — it is the easiest mistake to
        make on this form and the code is only good for ten minutes."""
        user = _sms_configured_user("mismatchuser")
        client, raw_code = _start_flow(user)
        for _ in range(MAX + 2):
            client.post("/accounts/forgot-password/verify/",
                        {"code": raw_code, "new_password": NEW_PASSWORD,
                         "confirm_password": "SomethingElse123!"})
        _guess(client, raw_code)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD),
                        "repeatedly mistyping the confirmation field destroyed "
                        "a perfectly good reset code")

    def test_the_user_is_told_how_many_tries_are_left(self):
        """A silent cap is indistinguishable from a broken form; the person
        typing needs to know the code is about to die so they can ask for a new
        one before it does."""
        user = _sms_configured_user("countdownuser")
        client, _ = _start_flow(user)
        response = _guess(client, "000000")
        body = response.content.decode()
        self.assertIn(f"{MAX - 1} more tries", body,
                      "the wrong-code message does not say how many tries "
                      f"remain (expected to see '{MAX - 1} more tries')")

    def test_the_two_layers_read_the_same_number(self):
        """The project's most-repeated lesson: one rule, one place. If the view
        ever grows its own literal, this fails."""
        from accounts import password_reset
        import inspect
        source = inspect.getsource(password_reset.SelfPasswordResetVerifyView)
        self.assertIn("MAX_VERIFY_ATTEMPTS", source,
                      "the verify view no longer reads the shared limit")
